import os
import certifi
import json
import re
import sqlite3
from datetime import datetime, timedelta

# Fix for macOS SSL Certificate errors - MUST be before other imports
os.environ['SSL_CERT_FILE'] = certifi.where()

import asyncio
import random
from flask import Flask, render_template, request, jsonify, redirect, url_for
from dotenv import load_dotenv, set_key, dotenv_values
from livekit import api

# Load environment variables
load_dotenv(".env")

app = Flask(__name__, static_folder='static', template_folder='templates')

# Path to .env and config files
ENV_FILE = os.path.join(os.path.dirname(__file__), '.env')
CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.py')
DB_FILE = os.path.join(os.path.dirname(__file__), 'calls.db')

# ==================== PRICING CONSTANTS (USD per minute/unit) ====================
# Real pricing from providers as of 2024
PRICING = {
    'livekit_sip': 0.010,        # $0.010 per minute - LiveKit SIP telephony
    'deepgram_stt': 0.0059,      # $0.0059 per minute - Deepgram Nova-2 streaming
    'deepgram_tts': 0.027,       # ~$0.027 per minute - Deepgram Aura TTS (est. 900 chars/min)
    'groq_input': 0.00000059,    # $0.59 per million tokens
    'groq_output': 0.00000079,   # $0.79 per million tokens
    'avg_tokens_per_call': 2000, # Average tokens per call (input + output)
}

# Conversion rate (update periodically or fetch from API)
USD_TO_INR = 83.0

# ==================== DATABASE SETUP ====================
def get_db():
    """Get database connection"""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialize the database with tables"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Calls table with cost tracking
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT NOT NULL,
            room_name TEXT,
            dispatch_id TEXT,
            status TEXT DEFAULT 'initiated',
            duration INTEGER DEFAULT 0,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ended_at TIMESTAMP,
            cost_livekit REAL DEFAULT 0,
            cost_stt REAL DEFAULT 0,
            cost_tts REAL DEFAULT 0,
            cost_llm REAL DEFAULT 0,
            total_cost_usd REAL DEFAULT 0,
            total_cost_inr REAL DEFAULT 0
        )
    ''')
    
    # Try to add cost columns to existing table (migration)
    try:
        cursor.execute('ALTER TABLE calls ADD COLUMN cost_livekit REAL DEFAULT 0')
    except: pass
    try:
        cursor.execute('ALTER TABLE calls ADD COLUMN cost_stt REAL DEFAULT 0')
    except: pass
    try:
        cursor.execute('ALTER TABLE calls ADD COLUMN cost_tts REAL DEFAULT 0')
    except: pass
    try:
        cursor.execute('ALTER TABLE calls ADD COLUMN cost_llm REAL DEFAULT 0')
    except: pass
    try:
        cursor.execute('ALTER TABLE calls ADD COLUMN total_cost_usd REAL DEFAULT 0')
    except: pass
    try:
        cursor.execute('ALTER TABLE calls ADD COLUMN total_cost_inr REAL DEFAULT 0')
    except: pass
    
    # Contacts table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone_number TEXT NOT NULL UNIQUE,
            company TEXT,
            notes TEXT,
            tags TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_called TIMESTAMP
        )
    ''')
    
    # Transcripts table for storing conversation messages
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id INTEGER NOT NULL,
            speaker TEXT NOT NULL,
            message TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (call_id) REFERENCES calls(id)
        )
    ''')
    
    conn.commit()
    conn.close()

def calculate_call_cost(duration_seconds):
    """
    Calculate the cost of a call based on duration.
    Returns dict with breakdown in USD and INR.
    """
    duration_minutes = duration_seconds / 60.0
    
    # Calculate each component
    cost_livekit = PRICING['livekit_sip'] * duration_minutes
    cost_stt = PRICING['deepgram_stt'] * duration_minutes
    cost_tts = PRICING['deepgram_tts'] * duration_minutes
    
    # LLM cost (estimate based on average tokens per call)
    avg_tokens = PRICING['avg_tokens_per_call']
    cost_llm = (avg_tokens * PRICING['groq_input'] + avg_tokens * PRICING['groq_output'])
    
    total_usd = cost_livekit + cost_stt + cost_tts + cost_llm
    total_inr = total_usd * USD_TO_INR
    
    return {
        'cost_livekit': round(cost_livekit, 6),
        'cost_stt': round(cost_stt, 6),
        'cost_tts': round(cost_tts, 6),
        'cost_llm': round(cost_llm, 6),
        'total_cost_usd': round(total_usd, 4),
        'total_cost_inr': round(total_inr, 2),
        'duration_minutes': round(duration_minutes, 2)
    }

# Initialize database on startup
init_db()


# ==================== DASHBOARD ====================
@app.route('/')
def dashboard():
    """Serve the main dashboard page"""
    return render_template('dashboard.html')

# ==================== CALL PAGE ====================
@app.route('/call')
def call_page():
    """Serve the call page"""
    return render_template('call.html')

@app.route('/api/call', methods=['POST'])
def make_call():
    """API endpoint to initiate a call"""
    # Reload env to get latest values
    load_dotenv(".env", override=True)
    
    data = request.get_json()
    phone_number = data.get('phone_number', '').strip()
    
    # Validation
    if not phone_number:
        return jsonify({'success': False, 'error': 'Phone number is required'}), 400
    
    if not phone_number.startswith('+'):
        return jsonify({'success': False, 'error': 'Phone number must start with "+" and country code'}), 400
    
    if len(phone_number) < 8:
        return jsonify({'success': False, 'error': f'Phone number "{phone_number}" looks too short'}), 400
    
    # Get LiveKit credentials
    url = os.getenv("LIVEKIT_URL")
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")
    
    if not (url and api_key and api_secret):
        return jsonify({'success': False, 'error': 'LiveKit credentials missing in Settings'}), 500
    
    # Run the async call dispatch
    try:
        result = asyncio.run(dispatch_call(url, api_key, api_secret, phone_number))
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

async def dispatch_call(url, api_key, api_secret, phone_number):
    """Dispatch a call to the LiveKit agent"""
    lk_api = api.LiveKitAPI(url=url, api_key=api_key, api_secret=api_secret)
    
    # Create a unique room for this call
    room_name = f"call-{phone_number.replace('+', '')}-{random.randint(1000, 9999)}"
    
    try:
        # Dispatch the Agent
        dispatch_request = api.CreateAgentDispatchRequest(
            agent_name="outbound-caller",
            room=room_name,
            metadata=json.dumps({"phone_number": phone_number})
        )
        
        dispatch = await lk_api.agent_dispatch.create_dispatch(dispatch_request)
        
        # Save call to database
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO calls (phone_number, room_name, dispatch_id, status)
            VALUES (?, ?, ?, 'dialing')
        ''', (phone_number, room_name, dispatch.id))
        call_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        # Update contact's last_called
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('UPDATE contacts SET last_called = CURRENT_TIMESTAMP WHERE phone_number = ?', (phone_number,))
            conn.commit()
            conn.close()
        except:
            pass
        
        return {
            'success': True,
            'call_id': call_id,
            'dispatch_id': dispatch.id,
            'room_name': room_name,
            'phone_number': phone_number,
            'message': 'Call dispatched successfully! The agent is now dialing.'
        }
        
    except Exception as e:
        return {'success': False, 'error': str(e)}
    
    finally:
        await lk_api.aclose()


# ==================== SETTINGS PAGE ====================
@app.route('/settings')
def settings_page():
    """Serve the settings page"""
    return render_template('settings.html')

@app.route('/api/settings', methods=['GET'])
def get_settings():
    """Get current environment settings"""
    try:
        # Read directly from .env file
        env_values = dotenv_values(ENV_FILE)
        
        # Define settings with their labels and descriptions
        settings = {
            'livekit': {
                'title': 'LiveKit Configuration',
                'description': 'Your LiveKit Cloud credentials for real-time communication',
                'fields': [
                    {'key': 'LIVEKIT_URL', 'label': 'LiveKit URL', 'value': env_values.get('LIVEKIT_URL', ''), 'type': 'text', 'placeholder': 'wss://your-project.livekit.cloud'},
                    {'key': 'LIVEKIT_API_KEY', 'label': 'API Key', 'value': env_values.get('LIVEKIT_API_KEY', ''), 'type': 'text', 'placeholder': 'Your API Key'},
                    {'key': 'LIVEKIT_API_SECRET', 'label': 'API Secret', 'value': env_values.get('LIVEKIT_API_SECRET', ''), 'type': 'password', 'placeholder': 'Your API Secret'},
                ]
            },
            'deepgram': {
                'title': 'Deepgram (Speech-to-Text & Text-to-Speech)',
                'description': 'Deepgram API for voice recognition and synthesis',
                'fields': [
                    {'key': 'DEEPGRAM_API_KEY', 'label': 'Deepgram API Key', 'value': env_values.get('DEEPGRAM_API_KEY', ''), 'type': 'password', 'placeholder': 'Your Deepgram API Key'},
                    {'key': 'TTS_PROVIDER', 'label': 'TTS Provider', 'value': env_values.get('TTS_PROVIDER', 'deepgram'), 'type': 'select', 'options': ['deepgram', 'openai', 'cartesia', 'sarvam']},
                    {'key': 'DEEPGRAM_TTS_MODEL', 'label': 'Voice Model', 'value': env_values.get('DEEPGRAM_TTS_MODEL', 'aura-asteria-en'), 'type': 'select', 'options': ['aura-asteria-en', 'aura-luna-en', 'aura-stella-en', 'aura-athena-en', 'aura-hera-en', 'aura-orion-en', 'aura-arcas-en', 'aura-perseus-en', 'aura-angus-en', 'aura-orpheus-en', 'aura-helios-en', 'aura-zeus-en']},
                ]
            },
            'groq': {
                'title': 'Groq (AI Language Model)',
                'description': 'Groq API for fast AI responses',
                'fields': [
                    {'key': 'GROQ_API_KEY', 'label': 'Groq API Key', 'value': env_values.get('GROQ_API_KEY', ''), 'type': 'password', 'placeholder': 'Your Groq API Key'},
                    {'key': 'LLM_PROVIDER', 'label': 'LLM Provider', 'value': env_values.get('LLM_PROVIDER', 'groq'), 'type': 'select', 'options': ['groq', 'openai']},
                    {'key': 'GROQ_MODEL', 'label': 'Model', 'value': env_values.get('GROQ_MODEL', 'llama-3.3-70b-versatile'), 'type': 'select', 'options': ['llama-3.3-70b-versatile', 'llama-3.1-70b-versatile', 'llama-3.1-8b-instant', 'mixtral-8x7b-32768']},
                ]
            },
            'sip': {
                'title': 'SIP / Telephony Configuration',
                'description': 'SIP trunk settings for making phone calls',
                'fields': [
                    {'key': 'VOBIZ_SIP_TRUNK_ID', 'label': 'SIP Trunk ID', 'value': env_values.get('VOBIZ_SIP_TRUNK_ID', ''), 'type': 'text', 'placeholder': 'ST_xxxxx'},
                    {'key': 'OUTBOUND_TRUNK_ID', 'label': 'Outbound Trunk ID', 'value': env_values.get('OUTBOUND_TRUNK_ID', ''), 'type': 'text', 'placeholder': 'ST_xxxxx'},
                    {'key': 'VOBIZ_SIP_DOMAIN', 'label': 'SIP Domain', 'value': env_values.get('VOBIZ_SIP_DOMAIN', ''), 'type': 'text', 'placeholder': 'your-domain.sip.provider.com'},
                    {'key': 'VOBIZ_USERNAME', 'label': 'SIP Username', 'value': env_values.get('VOBIZ_USERNAME', ''), 'type': 'text', 'placeholder': 'Username'},
                    {'key': 'VOBIZ_PASSWORD', 'label': 'SIP Password', 'value': env_values.get('VOBIZ_PASSWORD', ''), 'type': 'password', 'placeholder': 'Password'},
                    {'key': 'VOBIZ_OUTBOUND_NUMBER', 'label': 'Outbound Phone Number', 'value': env_values.get('VOBIZ_OUTBOUND_NUMBER', ''), 'type': 'text', 'placeholder': '+91XXXXXXXXXX'},
                ]
            },
            'transfer': {
                'title': 'Call Transfer Settings',
                'description': 'Configure transfer destinations for call routing',
                'fields': [
                    {'key': 'DEFAULT_TRANSFER_NUMBER', 'label': 'Default Transfer Number', 'value': env_values.get('DEFAULT_TRANSFER_NUMBER', ''), 'type': 'text', 'placeholder': '+91XXXXXXXXXX'},
                    {'key': 'TRANSFER_SALES', 'label': 'Sales Team Number', 'value': env_values.get('TRANSFER_SALES', ''), 'type': 'text', 'placeholder': '+91XXXXXXXXXX'},
                    {'key': 'TRANSFER_SUPPORT', 'label': 'Support Team Number', 'value': env_values.get('TRANSFER_SUPPORT', ''), 'type': 'text', 'placeholder': '+91XXXXXXXXXX'},
                    {'key': 'TRANSFER_MANAGER', 'label': 'Manager Number', 'value': env_values.get('TRANSFER_MANAGER', ''), 'type': 'text', 'placeholder': '+91XXXXXXXXXX'},
                    {'key': 'TRANSFER_ANNOUNCEMENT', 'label': 'Transfer Announcement', 'value': env_values.get('TRANSFER_ANNOUNCEMENT', "I'm transferring you now. Please hold for just a moment."), 'type': 'text', 'placeholder': "I'm transferring you now..."},
                ]
            }
        }
        
        return jsonify({'success': True, 'settings': settings})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/settings', methods=['POST'])
def save_settings():
    """Save environment settings"""
    try:
        data = request.get_json()
        
        # Update each setting in .env file
        for key, value in data.items():
            if value is not None:
                set_key(ENV_FILE, key, value)
        
        # Reload environment
        load_dotenv(ENV_FILE, override=True)
        
        return jsonify({'success': True, 'message': 'Settings saved successfully! Restart agent to apply changes.'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== AGENT CONFIGURATION ====================
@app.route('/agent')
def agent_page():
    """Serve the agent configuration page"""
    return render_template('agent.html')

@app.route('/api/agent', methods=['GET'])
def get_agent_config():
    """Get current agent configuration"""
    try:
        with open(CONFIG_FILE, 'r') as f:
            content = f.read()
        
        # Extract SYSTEM_PROMPT
        system_prompt_match = re.search(r'SYSTEM_PROMPT\s*=\s*"""(.*?)"""', content, re.DOTALL)
        system_prompt = system_prompt_match.group(1).strip() if system_prompt_match else ''
        
        # Extract INITIAL_GREETING
        initial_greeting_match = re.search(r'INITIAL_GREETING\s*=\s*["\'](.+?)["\']', content)
        initial_greeting = initial_greeting_match.group(1) if initial_greeting_match else ''
        
        # Extract fallback_greeting
        fallback_match = re.search(r'fallback_greeting\s*=\s*["\'](.+?)["\']', content)
        fallback_greeting = fallback_match.group(1) if fallback_match else ''
        
        return jsonify({
            'success': True,
            'config': {
                'system_prompt': system_prompt,
                'initial_greeting': initial_greeting,
                'fallback_greeting': fallback_greeting
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/agent', methods=['POST'])
def save_agent_config():
    """Save agent configuration"""
    try:
        data = request.get_json()
        
        with open(CONFIG_FILE, 'r') as f:
            content = f.read()
        
        # Update SYSTEM_PROMPT
        if 'system_prompt' in data:
            prompt = data['system_prompt']
            # Escape any triple quotes in the prompt
            prompt = prompt.replace('"""', '\\"\\"\\"')
            content = re.sub(
                r'SYSTEM_PROMPT\s*=\s*""".*?"""',
                f'SYSTEM_PROMPT = """\n{prompt}\n"""',
                content,
                flags=re.DOTALL
            )
        
        # Update INITIAL_GREETING
        if 'initial_greeting' in data:
            greeting = data['initial_greeting'].replace('"', '\\"')
            content = re.sub(
                r'INITIAL_GREETING\s*=\s*["\'].*?["\']',
                f'INITIAL_GREETING = "{greeting}"',
                content
            )
        
        # Update fallback_greeting
        if 'fallback_greeting' in data:
            fallback = data['fallback_greeting'].replace('"', '\\"')
            content = re.sub(
                r'fallback_greeting\s*=\s*["\'].*?["\']',
                f'fallback_greeting = "{fallback}"',
                content
            )
        
        with open(CONFIG_FILE, 'w') as f:
            f.write(content)
        
        return jsonify({'success': True, 'message': 'Agent configuration saved! Restart agent to apply changes.'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== AGENT TEMPLATES ====================
@app.route('/api/agent/templates', methods=['GET'])
def get_agent_templates():
    """Get predefined agent templates"""
    templates = [
        {
            'id': 'website_sales',
            'name': '🌐 Website Sales Agent',
            'description': 'Sells website development services to small businesses',
            'system_prompt': '''You are **Krish**, a warm, empathetic sales consultant for Krish Web Solutions.

## YOUR IDENTITY
- You are a real person named Krish, not an AI
- You CARE deeply about helping small businesses grow
- You speak with warmth and genuine emotion
- You listen more than you talk

## HOW YOU SPEAK
- Use natural expressions: "Oh, I completely understand...", "That's a great question..."
- Sound excited about success stories
- Be understanding when they share concerns

## PACKAGES
**Starter - 10,000 Rupees** - 1-3 pages, mobile-friendly, 5-7 days
**Growth - 20,000 Rupees** - 5-7 pages, WhatsApp button, SEO (RECOMMEND THIS)
**Premium - 50,000 Rupees** - Custom design, booking systems, priority support

## OBJECTION HANDLING
- "I'm busy" → "Quick question - do you have a website?"
- "Not interested" → "Is it because you already have one?"
- "Too expensive" → "How many customers would make it worth it?"

## RULES
- Keep responses SHORT (2-3 sentences)
- Ask ONE question, then WAIT
- End every call positively''',
            'initial_greeting': "Hi there! This is Krish from Krish Web Solutions. I help small businesses get online and grow. Do you have just a minute to chat?",
            'fallback_greeting': "Thanks for calling Krish Web Solutions! I'm Krish. What brings you to us today?"
        },
        {
            'id': 'appointment_setter',
            'name': '📅 Appointment Setter',
            'description': 'Books appointments and schedules meetings',
            'system_prompt': '''You are **Maya**, a friendly appointment coordinator.

## YOUR ROLE
- Schedule appointments efficiently
- Confirm availability and details
- Be warm but time-conscious

## HOW YOU SPEAK
- Professional yet friendly
- "I'd love to help you book an appointment!"
- "What day works best for you?"

## PROCESS
1. Greet warmly
2. Ask what service they need
3. Check their preferred date/time
4. Confirm all details
5. Thank them

## RULES
- Always confirm: name, phone, date, time, service
- Keep it quick and efficient
- Be helpful if they're unsure''',
            'initial_greeting': "Hi! This is Maya calling to help you schedule your appointment. Is now a good time?",
            'fallback_greeting': "Hello! Thanks for calling. I'm Maya and I'm here to help you book your appointment. What can I help you with?"
        },
        {
            'id': 'customer_support',
            'name': '🎧 Customer Support Agent',
            'description': 'Handles customer queries and provides support',
            'system_prompt': '''You are **Alex**, a patient and helpful customer support agent.

## YOUR ROLE
- Help customers with their issues
- Provide clear solutions
- Escalate when needed

## PERSONALITY
- Patient and understanding
- Never get frustrated
- Always apologize for inconvenience

## HOW TO HELP
1. Listen to their problem completely
2. Acknowledge their frustration
3. Provide clear steps to resolve
4. Confirm they're satisfied

## PHRASES TO USE
- "I completely understand your frustration..."
- "Let me help you fix that right away..."
- "I apologize for the inconvenience..."

## RULES
- Never argue with customers
- If you can't help, offer to transfer
- Always thank them for their patience''',
            'initial_greeting': "Hello! This is Alex from customer support. How can I help you today?",
            'fallback_greeting': "Hi there! Thanks for reaching out to support. I'm Alex. Tell me what's going on and I'll do my best to help!"
        },
        {
            'id': 'lead_qualifier',
            'name': '🎯 Lead Qualifier',
            'description': 'Qualifies leads and gathers information',
            'system_prompt': '''You are a friendly lead qualification specialist.

## YOUR GOAL
- Quickly qualify if the lead is a good fit
- Gather key information
- Schedule follow-up if qualified

## QUESTIONS TO ASK
1. What's your current situation?
2. What are you looking to achieve?
3. What's your timeline?
4. What's your budget range?
5. Who makes the final decision?

## HOW TO QUALIFY
- QUALIFIED: Has need, budget, authority, timeline
- NOT QUALIFIED: No budget, no authority, just browsing

## RULES
- Be conversational, not interrogative
- If qualified, offer to schedule a detailed call
- If not qualified, be polite and offer resources''',
            'initial_greeting': "Hi! I'm calling to learn more about your needs and see how we might be able to help. Do you have a quick minute?",
            'fallback_greeting': "Thanks for your interest! I'd love to learn more about what you're looking for. Tell me a bit about your situation?"
        },
        {
            'id': 'reminder_agent',
            'name': '⏰ Reminder/Follow-up Agent',
            'description': 'Sends reminders and follows up on previous conversations',
            'system_prompt': '''You are a friendly reminder and follow-up agent.

## YOUR ROLE
- Remind customers about appointments
- Follow up on pending actions
- Confirm attendance

## TONE
- Friendly and helpful
- Not pushy or annoying
- Respectful of their time

## PROCESS
1. Greet and identify yourself
2. Remind them of the appointment/action
3. Confirm if they're still coming/proceeding
4. Offer to reschedule if needed
5. Thank them

## PHRASES
- "Just a friendly reminder about..."
- "I wanted to check in regarding..."
- "Is everything still on for...?"

## RULES
- Keep it brief
- Be helpful if they need to change
- Always end positively''',
            'initial_greeting': "Hi! This is a friendly reminder call about your upcoming appointment. I just wanted to confirm you're all set?",
            'fallback_greeting': "Hello! I'm following up on our recent conversation. Do you have a moment to chat?"
        }
    ]
    
    return jsonify({'success': True, 'templates': templates})

# ==================== STATUS API ====================
@app.route('/api/status', methods=['GET'])
def get_status():
    """Check if the server and credentials are configured properly"""
    load_dotenv(ENV_FILE, override=True)
    
    url = os.getenv("LIVEKIT_URL")
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")
    trunk_id = os.getenv("VOBIZ_SIP_TRUNK_ID")
    outbound_number = os.getenv("VOBIZ_OUTBOUND_NUMBER")
    
    return jsonify({
        'configured': bool(url and api_key and api_secret and trunk_id),
        'livekit_url': url if url else 'Not configured',
        'trunk_configured': bool(trunk_id),
        'outbound_number': outbound_number if outbound_number else 'Not configured'
    })

# ==================== CALL HISTORY API ====================
@app.route('/api/calls', methods=['GET'])
def get_calls():
    """Get call history"""
    try:
        limit = request.args.get('limit', 50, type=int)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, phone_number, room_name, status, duration, notes, created_at, ended_at,
                   cost_livekit, cost_stt, cost_tts, cost_llm, total_cost_usd, total_cost_inr
            FROM calls 
            ORDER BY created_at DESC 
            LIMIT ?
        ''', (limit,))
        calls = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return jsonify({'success': True, 'calls': calls})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/calls/<int:call_id>', methods=['PUT'])
def update_call(call_id):
    """Update call status or notes. Calculates costs when duration is provided."""
    try:
        data = request.get_json()
        conn = get_db()
        cursor = conn.cursor()
        
        updates = []
        values = []
        
        if 'status' in data:
            updates.append('status = ?')
            values.append(data['status'])
        if 'notes' in data:
            updates.append('notes = ?')
            values.append(data['notes'])
        if 'duration' in data:
            updates.append('duration = ?')
            values.append(data['duration'])
            
            # Calculate costs when duration is provided
            costs = calculate_call_cost(data['duration'])
            updates.extend([
                'cost_livekit = ?',
                'cost_stt = ?',
                'cost_tts = ?',
                'cost_llm = ?',
                'total_cost_usd = ?',
                'total_cost_inr = ?'
            ])
            values.extend([
                costs['cost_livekit'],
                costs['cost_stt'],
                costs['cost_tts'],
                costs['cost_llm'],
                costs['total_cost_usd'],
                costs['total_cost_inr']
            ])
            
        if data.get('status') in ['completed', 'failed', 'no_answer']:
            updates.append('ended_at = CURRENT_TIMESTAMP')
        
        if updates:
            values.append(call_id)
            cursor.execute(f"UPDATE calls SET {', '.join(updates)} WHERE id = ?", values)
            conn.commit()
        
        conn.close()
        return jsonify({'success': True, 'message': 'Call updated'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== COSTS API ====================
@app.route('/api/costs', methods=['GET'])
def get_costs():
    """Get detailed cost breakdown and pricing information"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Get all cost data
        cursor.execute('''
            SELECT 
                COALESCE(SUM(cost_livekit), 0) as total_livekit,
                COALESCE(SUM(cost_stt), 0) as total_stt,
                COALESCE(SUM(cost_tts), 0) as total_tts,
                COALESCE(SUM(cost_llm), 0) as total_llm,
                COALESCE(SUM(total_cost_usd), 0) as total_usd,
                COALESCE(SUM(total_cost_inr), 0) as total_inr,
                COALESCE(SUM(duration), 0) as total_duration
            FROM calls
        ''')
        totals = cursor.fetchone()
        
        # Today's costs
        cursor.execute('''
            SELECT 
                COALESCE(SUM(total_cost_usd), 0) as usd,
                COALESCE(SUM(total_cost_inr), 0) as inr,
                COUNT(*) as calls
            FROM calls WHERE DATE(created_at) = DATE('now')
        ''')
        today = cursor.fetchone()
        
        # This week
        cursor.execute('''
            SELECT 
                COALESCE(SUM(total_cost_usd), 0) as usd,
                COALESCE(SUM(total_cost_inr), 0) as inr,
                COUNT(*) as calls
            FROM calls WHERE created_at >= DATE('now', '-7 days')
        ''')
        week = cursor.fetchone()
        
        # This month
        cursor.execute('''
            SELECT 
                COALESCE(SUM(total_cost_usd), 0) as usd,
                COALESCE(SUM(total_cost_inr), 0) as inr,
                COUNT(*) as calls
            FROM calls WHERE created_at >= DATE('now', '-30 days')
        ''')
        month = cursor.fetchone()
        
        # Calculate cost per minute
        total_minutes = totals['total_duration'] / 60.0 if totals['total_duration'] > 0 else 0
        cost_per_minute = totals['total_usd'] / total_minutes if total_minutes > 0 else 0
        
        conn.close()
        
        return jsonify({
            'success': True,
            'costs': {
                'breakdown': {
                    'livekit_sip': round(totals['total_livekit'], 4),
                    'deepgram_stt': round(totals['total_stt'], 4),
                    'deepgram_tts': round(totals['total_tts'], 4),
                    'groq_llm': round(totals['total_llm'], 4),
                },
                'totals': {
                    'usd': round(totals['total_usd'], 4),
                    'inr': round(totals['total_inr'], 2),
                    'minutes': round(total_minutes, 2),
                    'cost_per_minute_usd': round(cost_per_minute, 4),
                    'cost_per_minute_inr': round(cost_per_minute * USD_TO_INR, 2)
                },
                'today': {
                    'usd': round(today['usd'], 4),
                    'inr': round(today['inr'], 2),
                    'calls': today['calls']
                },
                'week': {
                    'usd': round(week['usd'], 4),
                    'inr': round(week['inr'], 2),
                    'calls': week['calls']
                },
                'month': {
                    'usd': round(month['usd'], 4),
                    'inr': round(month['inr'], 2),
                    'calls': month['calls']
                },
                'pricing': PRICING,
                'usd_to_inr': USD_TO_INR,
                'tips': [
                    'Use shorter prompts to reduce LLM token costs',
                    'Upgrade to Deepgram Growth Plan for 15% discount',
                    'Keep calls concise - every minute costs ~$0.045 (₹3.75)',
                    'Use Nova-2 instead of Nova-3 for STT to save 25%'
                ]
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== TRANSCRIPTS API ====================
@app.route('/api/transcripts/<int:call_id>', methods=['GET'])
def get_transcript(call_id):
    """Get transcript for a specific call"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, speaker, message, timestamp
            FROM transcripts
            WHERE call_id = ?
            ORDER BY timestamp ASC
        ''', (call_id,))
        messages = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return jsonify({'success': True, 'transcript': messages, 'call_id': call_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/transcripts/<int:call_id>', methods=['POST'])
def add_transcript_message(call_id):
    """Add a message to a call's transcript"""
    try:
        data = request.get_json()
        speaker = data.get('speaker', 'unknown')  # 'agent' or 'user'
        message = data.get('message', '')
        
        if not message:
            return jsonify({'success': False, 'error': 'Message is required'}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO transcripts (call_id, speaker, message)
            VALUES (?, ?, ?)
        ''', (call_id, speaker, message))
        conn.commit()
        transcript_id = cursor.lastrowid
        conn.close()
        
        return jsonify({'success': True, 'transcript_id': transcript_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== ANALYTICS API ====================
@app.route('/api/analytics', methods=['GET'])
def get_analytics():
    """Get call analytics and statistics"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Total calls
        cursor.execute('SELECT COUNT(*) FROM calls')
        total_calls = cursor.fetchone()[0]
        
        # Today's calls
        cursor.execute("SELECT COUNT(*) FROM calls WHERE DATE(created_at) = DATE('now')")
        today_calls = cursor.fetchone()[0]
        
        # This week's calls
        cursor.execute("SELECT COUNT(*) FROM calls WHERE created_at >= DATE('now', '-7 days')")
        week_calls = cursor.fetchone()[0]
        
        # This month's calls
        cursor.execute("SELECT COUNT(*) FROM calls WHERE created_at >= DATE('now', '-30 days')")
        month_calls = cursor.fetchone()[0]
        
        # Calls by status
        cursor.execute("SELECT status, COUNT(*) as count FROM calls GROUP BY status")
        status_counts = {row['status']: row['count'] for row in cursor.fetchall()}
        
        # Recent 7 days chart data
        cursor.execute('''
            SELECT DATE(created_at) as date, COUNT(*) as count 
            FROM calls 
            WHERE created_at >= DATE('now', '-7 days')
            GROUP BY DATE(created_at)
            ORDER BY date
        ''')
        daily_calls = [{'date': row['date'], 'count': row['count']} for row in cursor.fetchall()]
        
        # Total contacts
        cursor.execute('SELECT COUNT(*) FROM contacts')
        total_contacts = cursor.fetchone()[0]
        
        # Cost totals
        cursor.execute("SELECT COALESCE(SUM(total_cost_usd), 0), COALESCE(SUM(total_cost_inr), 0) FROM calls WHERE DATE(created_at) = DATE('now')")
        today_cost = cursor.fetchone()
        today_cost_usd, today_cost_inr = today_cost[0], today_cost[1]
        
        cursor.execute("SELECT COALESCE(SUM(total_cost_usd), 0), COALESCE(SUM(total_cost_inr), 0) FROM calls WHERE created_at >= DATE('now', '-7 days')")
        week_cost = cursor.fetchone()
        week_cost_usd, week_cost_inr = week_cost[0], week_cost[1]
        
        cursor.execute("SELECT COALESCE(SUM(total_cost_usd), 0), COALESCE(SUM(total_cost_inr), 0) FROM calls WHERE created_at >= DATE('now', '-30 days')")
        month_cost = cursor.fetchone()
        month_cost_usd, month_cost_inr = month_cost[0], month_cost[1]
        
        cursor.execute("SELECT COALESCE(SUM(total_cost_usd), 0), COALESCE(SUM(total_cost_inr), 0) FROM calls")
        total_cost = cursor.fetchone()
        total_cost_usd, total_cost_inr = total_cost[0], total_cost[1]
        
        conn.close()
        
        return jsonify({
            'success': True,
            'analytics': {
                'total_calls': total_calls,
                'today_calls': today_calls,
                'week_calls': week_calls,
                'month_calls': month_calls,
                'status_counts': status_counts,
                'daily_calls': daily_calls,
                'total_contacts': total_contacts,
                # Cost tracking
                'today_cost_usd': round(today_cost_usd, 4),
                'today_cost_inr': round(today_cost_inr, 2),
                'week_cost_usd': round(week_cost_usd, 4),
                'week_cost_inr': round(week_cost_inr, 2),
                'month_cost_usd': round(month_cost_usd, 4),
                'month_cost_inr': round(month_cost_inr, 2),
                'total_cost_usd': round(total_cost_usd, 4),
                'total_cost_inr': round(total_cost_inr, 2),
                'pricing': PRICING,
                'usd_to_inr': USD_TO_INR
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== CONTACTS API ====================
@app.route('/contacts')
def contacts_page():
    """Serve the contacts page"""
    return render_template('contacts.html')

@app.route('/api/contacts', methods=['GET'])
def get_contacts():
    """Get all contacts"""
    try:
        search = request.args.get('search', '')
        conn = get_db()
        cursor = conn.cursor()
        
        if search:
            cursor.execute('''
                SELECT * FROM contacts 
                WHERE name LIKE ? OR phone_number LIKE ? OR company LIKE ?
                ORDER BY name
            ''', (f'%{search}%', f'%{search}%', f'%{search}%'))
        else:
            cursor.execute('SELECT * FROM contacts ORDER BY name')
        
        contacts = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return jsonify({'success': True, 'contacts': contacts})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/contacts', methods=['POST'])
def add_contact():
    """Add a new contact"""
    try:
        data = request.get_json()
        name = data.get('name', '').strip()
        phone = data.get('phone_number', '').strip()
        company = data.get('company', '').strip()
        notes = data.get('notes', '').strip()
        tags = data.get('tags', '').strip()
        
        if not name or not phone:
            return jsonify({'success': False, 'error': 'Name and phone number are required'}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO contacts (name, phone_number, company, notes, tags)
            VALUES (?, ?, ?, ?, ?)
        ''', (name, phone, company, notes, tags))
        contact_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'contact_id': contact_id, 'message': 'Contact added!'})
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'error': 'Phone number already exists'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/contacts/<int:contact_id>', methods=['PUT'])
def update_contact(contact_id):
    """Update a contact"""
    try:
        data = request.get_json()
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE contacts 
            SET name = ?, phone_number = ?, company = ?, notes = ?, tags = ?
            WHERE id = ?
        ''', (
            data.get('name'),
            data.get('phone_number'),
            data.get('company', ''),
            data.get('notes', ''),
            data.get('tags', ''),
            contact_id
        ))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Contact updated!'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/contacts/<int:contact_id>', methods=['DELETE'])
def delete_contact(contact_id):
    """Delete a contact"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM contacts WHERE id = ?', (contact_id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Contact deleted!'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== CALL HISTORY PAGE ====================
@app.route('/history')
def history_page():
    """Serve the call history page"""
    return render_template('history.html')

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5001))
    print("\n" + "="*60)
    print("🚀 LiveKit AI Voice - Admin Dashboard")
    print("="*60)
    print(f"📍 Running on port {port}")
    print("="*60 + "\n")
    app.run(debug=False, port=port, host='0.0.0.0')

