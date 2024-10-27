import discord
from discord.ext import tasks
from datetime import datetime, timezone
import os
import asyncio
from google.oauth2 import service_account
from googleapiclient.discovery import build
from dotenv import load_dotenv

load_dotenv()
# German months dictionary
german_months = {
    1: "Januar", 2: "Februar", 3: "März", 4: "April", 5: "Mai", 6: "Juni",
    7: "Juli", 8: "August", 9: "September", 10: "Oktober", 11: "November", 12: "Dezember"
}

# Google Tasks API Authentication
def authenticate_google_tasks():
    service_account_file = 'service_account.json'
    if not os.path.exists(service_account_file):
        raise Exception(f"Service account file {service_account_file} not found.")
    creds = service_account.Credentials.from_service_account_file(service_account_file)
    return build('tasks', 'v1', credentials=creds)

# Get Tasklist ID by Name
def get_tasklist_id_by_name(service, tasklist_name):
    result = service.tasklists().list().execute()
    tasklists = result.get('items', [])
    for tasklist in tasklists:
        if tasklist['title'].lower() == tasklist_name.lower():
            return tasklist['id']
    raise ValueError(f"Tasklist with name '{tasklist_name}' not found.")

# Discord Bot Token
token = os.getenv('DISCORD_TOKEN')

# Instantiate the Google Tasks service
service = authenticate_google_tasks()

# Get the Tasklist ID from its name
tasklist_name = "My Tasks"
tasklist_id = get_tasklist_id_by_name(service, tasklist_name)

# Discord Bot Initialization
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.message_content = True
bot = discord.Client(intents=intents)

pinned_message_id = None  # Store the pinned message ID to update it

# Function to get tasks from Google Tasks API
def get_tasks(service, tasklist_id):
    result = service.tasks().list(tasklist=tasklist_id).execute()
    tasks = result.get('items', [])
    return tasks

# Function to get pending tasks
def get_pending_tasks(service, tasklist_id):
    tasks = get_tasks(service, tasklist_id)
    pending_tasks = []
    now = datetime.now(timezone.utc)

    for task in tasks:
        due_date = task.get('due')
        if task.get('status') != 'completed':
            if due_date:
                due_datetime = datetime.fromisoformat(due_date[:-1] + '+00:00')
                if due_datetime > now:
                    pending_tasks.append({
                        'title': task['title'],
                        'due_date': due_datetime.strftime('%Y-%m-%d')
                    })
            else:
                pending_tasks.append({
                    'title': task['title'],
                    'due_date': 'No due date'
                })

    return pending_tasks

# Function to get passed tasks
def get_passed_tasks(service, tasklist_id):
    tasks = get_tasks(service, tasklist_id)
    passed_tasks = []
    now = datetime.now(timezone.utc)

    for task in tasks:
        due_date = task.get('due')
        if task.get('status') != 'completed':
            if due_date:
                due_datetime = datetime.fromisoformat(due_date[:-1] + '+00:00')
                if due_datetime < now:
                    passed_tasks.append({
                        'title': task['title'],
                        'due_date': due_datetime.strftime('%Y-%m-%d')
                    })

    return passed_tasks

# Function to display tasks overview
def display_tasks(service, tasklist_id):
    pending_tasks = get_pending_tasks(service, tasklist_id)
    passed_tasks = get_passed_tasks(service, tasklist_id)

    def format_date(date_str):
        date_obj = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        return f"{date_obj.day}. {german_months[date_obj.month]}"

    pending_tasks_md = '\n'.join([f"- **{task['title']}** (Fällig: {format_date(task['due_date'])})" for task in pending_tasks])
    passed_tasks_md = '\n'.join([f"- __{task['title']}__ (War fällig: {format_date(task['due_date'])})" for task in passed_tasks])

    message = (
        "### Aufgabenübersicht\n"
        f"**Ausstehende Aufgaben:**\n{pending_tasks_md if pending_tasks_md else 'Keine ausstehenden Aufgaben.'}\n\n"
        f"**Vergangene Aufgaben:**\n{passed_tasks_md if passed_tasks_md else 'Keine vergangenen Aufgaben.'}\n\n"
    )
    
    return message

# Task loop to update tasks overview
@tasks.loop(seconds=10)
async def update_tasks():
    global pinned_message_id
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.name == 'tasks':
                # Fetch the current pinned message
                if pinned_message_id is None:
                    async for msg in channel.history(limit=10):
                        if msg.pinned and msg.author == bot.user and msg.content.startswith('### Aufgabenübersicht'):
                            pinned_message_id = msg.id
                            break

                # Get the latest tasks overview
                tasks_overview = display_tasks(service, tasklist_id)

                # If we have a pinned message, update it
                if pinned_message_id:
                    pinned_message = await channel.fetch_message(pinned_message_id)
                    await pinned_message.edit(content=tasks_overview)
                else:
                    # If no pinned message exists, create a new one
                    bot_message = await channel.send(tasks_overview)
                    await bot_message.pin()
                    pinned_message_id = bot_message.id

# Event to handle bot readiness
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    update_tasks.start()  # Start updating tasks every 10 seconds

# Run the bot
async def start_bot():
    await bot.start(token)

# Start the bot
if __name__ == "__main__":
    asyncio.run(start_bot())
