import os
import time
import pickle
import asyncio
from datetime import datetime, timezone
from typing import Optional, Type
from cryptography.fernet import Fernet
from flask import Flask, request, jsonify
import threading

import dateutil.parser
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import discord
from discord.ext import tasks
import nest_asyncio
from pydantic import BaseModel, Field

from langchain.tools import BaseTool
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
import locale

# Set locale to German
locale.setlocale(locale.LC_TIME, 'de_DE.UTF-8')

app = Flask(__name__)

@app.route('/')
def health_check():
    print("Health check endpoint called")
    return "Health Check OK", 200

def run_flask():
    print("Starting Flask app")
    app.run(host='0.0.0.0', port=8000)

# Scopes allow us to read and write tasks
SCOPES = ['https://www.googleapis.com/auth/tasks']
load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
CHANNEL_NAME = 'beta_hausaufgaben'
azure_token = os.getenv("AZURE_TOKEN")
encryption_key = os.environ.get('ENCRYPTION_KEY').encode()

# Decrypt token.pickle
def decrypt_token():
    print("Decrypting token.pickle")
    with open("token.pickle.encrypted", "rb") as encrypted_file:
        encrypted_data = encrypted_file.read()
        print("Read encrypted token data")

    fernet = Fernet(encryption_key)
    decrypted_data = fernet.decrypt(encrypted_data)
    print("Decrypted token data")

    with open("token.pickle", "wb") as decrypted_file:
        decrypted_file.write(decrypted_data)
        print("Decrypted token saved to token.pickle")

# Authenticate using OAuth tokens
def authenticate_google_tasks():
    print("Authenticating with Google Tasks API")
    creds = None
    
    # Decrypt and load credentials from the token.pickle file
    decrypt_token()

    # Load the token from token.pickle on the cloud environment
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token_file:
            creds = pickle.load(token_file)
            print("Token loaded successfully.")
    
    # If the credentials are expired, refresh them
    if creds and creds.expired and creds.refresh_token:
        print("Access token expired, refreshing...")
        creds.refresh(Request())

    if not creds:
        raise Exception("No valid credentials found. You need to authenticate locally first.")

    # Return authenticated Google Tasks API service
    print("Building Google Tasks service")
    return build('tasks', 'v1', credentials=creds)


# Get Task List ID by Task List Title
def get_tasklist_id_by_title(service, title):
    print(f"Fetching tasklist ID for title: {title}")
    result = service.tasklists().list().execute()
    tasklists = result.get('items', [])
    for tasklist in tasklists:
        print(f"Checking tasklist: {tasklist['title']}")
        if tasklist['title'].lower() == title.lower():
            print(f"Found matching tasklist ID: {tasklist['id']}")
            return tasklist['id']
    raise ValueError(f"Tasklist '{title}' not found")

# Fetch all tasks from the tasklist
def get_tasks(service, tasklist_id):
    print(f"Fetching tasks for tasklist ID: {tasklist_id}")
    result = service.tasks().list(tasklist=tasklist_id).execute()
    tasks = result.get('items', [])
    print(f"Retrieved {len(tasks)} tasks")
    return tasks

# Fetch pending tasks (tasks that are not completed)
def get_pending_tasks(service, tasklist_id):
    print("Getting pending tasks")
    tasks = get_tasks(service, tasklist_id)
    pending_tasks = []
    now = datetime.now(timezone.utc)
    print(f"Current time: {now.isoformat()}")

    for task in tasks:
        due_date = task.get('due')
        print(f"Processing task: {task['title']}, due date: {due_date}")
        if task.get('status') != 'completed':  # Only display tasks that are not completed
            if due_date:
                due_datetime = datetime.fromisoformat(due_date[:-1] + '+00:00')
                print(f"Task due datetime: {due_datetime.isoformat()}")
                if due_datetime > now:
                    pending_tasks.append({
                        'title': task['title'],
                        'due_date': due_datetime.strftime('%Y-%m-%d')
                    })
                    print(f"Added pending task: {task['title']}")
            else:
                # Include tasks without a due date in pending tasks
                pending_tasks.append({
                    'title': task['title'],
                    'due_date': 'No due date'
                })
                print(f"Added pending task without due date: {task['title']}")

    print(f"Total pending tasks: {len(pending_tasks)}")
    return pending_tasks

# Fetch passed tasks (tasks where the due date has passed and they are not marked as completed)
def get_passed_tasks(service, tasklist_id):
    print("Getting passed tasks")
    tasks = get_tasks(service, tasklist_id)
    passed_tasks = []
    now = datetime.now(timezone.utc)
    print(f"Current time: {now.isoformat()}")

    for task in tasks:
        due_date = task.get('due')
        print(f"Processing task: {task['title']}, due date: {due_date}")
        if task.get('status') != 'completed':  # Only consider non-completed tasks
            if due_date:
                due_datetime = datetime.fromisoformat(due_date[:-1] + '+00:00')
                print(f"Task due datetime: {due_datetime.isoformat()}")
                if due_datetime < now:
                    passed_tasks.append({
                        'title': task['title'],
                        'due_date': due_datetime.strftime('%Y-%m-%d')
                    })
                    print(f"Added passed task: {task['title']}")
    print(f"Total passed tasks: {len(passed_tasks)}")
    return passed_tasks

def display_tasks(service, tasklist_id):
    print("Displaying tasks")
    pending_tasks = get_pending_tasks(service, tasklist_id)
    passed_tasks = get_passed_tasks(service, tasklist_id)

    def format_date(date_str):
        date_obj = datetime.strptime(date_str, '%Y-%m-%dT%H:%M:%S.%fZ')
        return date_obj.strftime('%d. %B')

    pending_tasks_md = '\n'.join([f"- **{task['title']}** (Fällig: {format_date(task['due_date'])})" for task in pending_tasks])
    passed_tasks_md = '\n'.join([f"- __{task['title']}__ (War fällig: {format_date(task['due_date'])})" for task in passed_tasks])

    message = (
        "### Aufgabenübersicht\n"
        f"**Ausstehende Aufgaben:**\n{pending_tasks_md if pending_tasks_md else 'Keine ausstehenden Aufgaben.'}\n\n"
        f"**Vergangene Aufgaben:**\n{passed_tasks_md if passed_tasks_md else 'Keine vergangenen Aufgaben.'}\n\n"
    )
    
    print("Generated tasks overview message")
    return message

# Define the input schema for creating a task
class CreateTaskInput(BaseModel):
    task_title: str = Field(description="Title of the task to create")
    due_date: Optional[str] = Field(default=None, description="Due date in RFC3339 or other common date formats, None if no due date")
    priority: Optional[str] = Field(default=None, description="Priority level of the task")
    description: Optional[str] = Field(default=None, description="Description of the task")

# Define the custom tool for creating a task in Google Tasks
class CreateTaskTool(BaseTool):
    name: str = "create_task"
    description: str = "Tool for creating a new task in Google Tasks."
    args_schema: Type[BaseModel] = CreateTaskInput
    return_direct: bool = False

    def _run(
        self, task_title: str, due_date: Optional[str] = None, priority: Optional[str] = None, description: Optional[str] = None, run_manager: Optional = None
    ) -> str:
        """Create a new task in Google Tasks."""
        print(f"Running CreateTaskTool with task_title: {task_title}, due_date: {due_date}, priority: {priority}, description: {description}")
        # Authenticate and get the Google Tasks service
        service = authenticate_google_tasks()

        # Get the task list ID
        tasklist_id = get_tasklist_id_by_title(service, "Schule")
        print(f"Using tasklist ID: {tasklist_id}")

        # Parse the due date if provided
        parsed_due_date = None
        if due_date:
            try:
                parsed_due_date = datetime.fromisoformat(due_date).isoformat()
                print(f"Parsed due date: {parsed_due_date}")
            except ValueError:
                try:
                    parsed_due_date = dateutil.parser.parse(due_date).isoformat()
                    print(f"Parsed due date with dateutil.parser: {parsed_due_date}")
                except ValueError:
                    print("Invalid due date format")
                    raise ValueError("Invalid due date format. Please provide a valid date.")

        # Prepare the task body
        task_body = {'title': task_title}
        if parsed_due_date:
            task_body['due'] = parsed_due_date
        if priority:
            task_body['notes'] = f"Priority: {priority}"
        if description:
            task_body['notes'] = (task_body.get('notes', '') + f"\nDescription: {description}").strip()
        print(f"Task body prepared: {task_body}")

        # Create the task
        task = service.tasks().insert(tasklist=tasklist_id, body=task_body).execute()
        print(f"Created task with ID: {task['id']}")
        return f"Created task '{task_title}' with ID: {task['id']}"

# Define the input schema for getting the current date
class GetCurrentDateInput(BaseModel):
    format: Optional[str] = Field(default="RFC3339", description="Format in which to return the current date (e.g., RFC3339)")

# Define the custom tool for getting the current date
class GetCurrentDateTool(BaseTool):
    name: str = "get_current_date"
    description: str = "Tool for retrieving the current date in the specified format."
    args_schema: Type[BaseModel] = GetCurrentDateInput
    return_direct: bool = False

    def _run(self, format: Optional[str] = "RFC3339", run_manager: Optional = None) -> str:
        """Get the current date in the specified format."""
        print(f"Running GetCurrentDateTool with format: {format}")
        current_date = datetime.now(timezone.utc)
        if format == "RFC3339":
            date_str = current_date.isoformat()
        else:
            date_str = current_date.strftime(format)
        print(f"Current date: {date_str}")
        return date_str

# Instantiate the tools
create_task_tool = CreateTaskTool()
get_current_date_tool = GetCurrentDateTool()

# Mycroft agent configuration
model_name = "gpt-4o-mini"
endpoint = "https://models.inference.ai.azure.com"
system_prompt = """You are an MYcroft-mini a assistant that helps students create Google Tasks for their homework. Process each assignment, 
create a corresponding task with all provided details, and fit short info into the title. 
Determine exact due dates from relative terms using the current date in RFC3339 format None if no due date is given. 
Debug and retry if issues arise. Always use the same language as the user."""

# Initialize the language model
print("Initializing language model")
llm = ChatOpenAI(
    model_name=model_name,
    base_url=endpoint,
    api_key=azure_token
)

# Initialize memory saver
print("Initializing memory saver")
memory = MemorySaver()

# Create the agent executor
print("Creating agent executor")
tools = [get_current_date_tool, create_task_tool]
agent_executor = create_react_agent(
    llm, tools, checkpointer=memory, state_modifier=system_prompt
)

# Initialize the Discord Bot
print("Initializing Discord bot")
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.message_content = True

bot = discord.Client(intents=intents)
service = authenticate_google_tasks()
tasklist_id = None
pinned_message_id = None  # Store the pinned message ID to update it


# Example function to send a message to the agent
def agent_send_message(message):
    print(f"Sending message to agent: {message}")
    human_message = HumanMessage(content=message)
    response = agent_executor.invoke(
        {"messages": [human_message]},
        config={"configurable": {"thread_id": "default", "recursion_limit": 1000}},
    )
    print("Agent response received")
    return response

# Function to extract the most recent message content and tool calls from the agent's response
def get_most_recent_ai_message_content_and_tool_calls(response):
    print("Extracting most recent AI message content and tool calls")
    messages = response.get('messages', [])
    most_recent_content = None
    tool_calls = []

    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        if isinstance(message, AIMessage):
            if message.content:
                most_recent_content = message.content
                print(f"Found AI message content: {most_recent_content}")
            if 'tool_calls' in message.additional_kwargs:
                tool_calls.extend(message.additional_kwargs.get('tool_calls', []))
                print(f"Found tool calls: {tool_calls}")

    return most_recent_content, tool_calls

@bot.event
async def on_ready():
    global tasklist_id
    print(f'Logged in as {bot.user}')
    print("Deleting non-pinned messages")
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.name == CHANNEL_NAME:
                try:
                    async for msg in channel.history(limit=None):
                        if not msg.pinned and not msg.content.startswith('### Pinned Tasks'):
                            print(f"Deleting message: {msg.content}")
                            await msg.delete()
                except discord.Forbidden:
                    print(f"Permission error: Cannot delete messages in {channel.name}")
                except discord.HTTPException as e:
                    print(f"HTTP error: {e} while deleting messages in {channel.name}")
                except Exception as e:
                    print(f"Unexpected error: {e} while deleting messages in {channel.name}")
    tasklist_id = get_tasklist_id_by_title(service, "Schule")  # Set the task list ID
    print(f"Tasklist ID set: {tasklist_id}")
    update_tasks.start()  # Start updating tasks every minute

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.channel.name == CHANNEL_NAME:
        content = message.content.strip().lower()
        print(f"Received message: {content}")

        if content.startswith('/task-history'):
            print("Processing /task-history command")
            bot_message = await message.channel.send(f"### Last 10 Completed Tasks\n TODO: Implement this feature")
            await asyncio.sleep(10)
            await bot_message.delete()
            await message.delete()
            return

        # Pass the user message to the agent
        print("Passing message to agent")
        response = agent_send_message(message.content)
        agent_message, tool_calls = get_most_recent_ai_message_content_and_tool_calls(response)

        # Send agent response back to the Discord channel
        print(f"Sending agent response: {agent_message}")
        bot_message = await message.channel.send(f"**Agent Response:** {agent_message}")
        await asyncio.sleep(30)  # Optionally delete messages after 30 seconds
        await message.delete()
        await bot_message.delete()

@tasks.loop(seconds=10)  # Loop to update tasks every 10 seconds
async def update_tasks():
    global pinned_message_id
    print("Updating tasks")
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.name == CHANNEL_NAME:
                print(f"Updating tasks in channel: {channel.name}")
                # Fetch the current pinned message
                if pinned_message_id is None:
                    print("No pinned message ID, searching for pinned message")
                    async for msg in channel.history(limit=10):
                        if msg.pinned and msg.author == bot.user and msg.content.startswith('### Aufgabenübersicht'):
                            pinned_message_id = msg.id
                            print(f"Found pinned message with ID: {pinned_message_id}")
                            break

                # Get the latest tasks overview
                tasks_overview = display_tasks(service, tasklist_id)

                # If we have a pinned message, update it
                if pinned_message_id:
                    print(f"Updating pinned message ID: {pinned_message_id}")
                    pinned_message = await channel.fetch_message(pinned_message_id)
                    await pinned_message.edit(content=tasks_overview)
                else:
                    # If no pinned message exists, create a new one
                    print("Creating new pinned message")
                    bot_message = await channel.send(tasks_overview)
                    await bot_message.pin()
                    pinned_message_id = bot_message.id


async def start_bot():
    print("Starting bot")
    await bot.start(TOKEN)

def run_bot():
    print("Running bot")
    asyncio.run(start_bot())

# Create and start the threads
print("Starting threads")
bot_thread = threading.Thread(target=run_bot)
flask_thread = threading.Thread(target=run_flask)

bot_thread.start()
flask_thread.start()

# Join the threads to ensure they run concurrently
bot_thread.join()
flask_thread.join()
