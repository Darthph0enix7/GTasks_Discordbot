import os
import pickle
import threading
from datetime import datetime, timezone
from typing import Optional, Type
from cryptography.fernet import Fernet
from flask import Flask
import dateutil.parser
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import discord
from discord.ext import tasks
from pydantic import BaseModel, Field
import asyncio

from langchain.tools import BaseTool
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

# Load environment variables
load_dotenv()

# Flask application for health checks
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Health Check OK", 200

def run_flask():
    print("Starting Flask server...")
    app.run(host='0.0.0.0', port=8000)

# Discord bot and Google API credentials
TOKEN = os.getenv('DISCORD_TOKEN')
CHANNEL_NAME = 'hausaufgaben'
azure_token = os.getenv("AZURE_TOKEN")
encryption_key = os.getenv('ENCRYPTION_KEY').encode()

# Google Tasks API scopes
SCOPES = ['https://www.googleapis.com/auth/tasks']

# Global variables
service = None

# Decrypt token.pickle
def decrypt_token():
    with open("token.pickle.encrypted", "rb") as encrypted_file:
        print("Loading and decrypting token.pickle.encrypted...")
        encrypted_data = encrypted_file.read()

    fernet = Fernet(encryption_key)
    decrypted_data = fernet.decrypt(encrypted_data)

    with open("token.pickle", "wb") as decrypted_file:
        decrypted_file.write(decrypted_data)
        print("Decrypted token saved to token.pickle")

# Authenticate using OAuth tokens
def authenticate_cloud():
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
    return build('tasks', 'v1', credentials=creds)

# Get Task List ID by Task List Title
def get_tasklist_id_by_title(service, title):
    print(f"Getting task list ID for title: {title}")
    result = service.tasklists().list().execute()
    tasklists = result.get('items', [])
    
    if not tasklists:
        print("No task lists found.")
    else:
        print("Available task lists:")
        for tasklist in tasklists:
            print(f"Task List Title: {tasklist['title']}, ID: {tasklist['id']}")

    for tasklist in tasklists:
        if tasklist['title'].lower() == title.lower():
            print(f"Found task list ID: {tasklist['id']}")
            return tasklist['id']
    
    raise ValueError(f"Tasklist '{title}' not found")

# Fetch tasks from the tasklist
def get_tasks(service, tasklist_id):
    print(f"Fetching tasks for tasklist ID: {tasklist_id}")
    result = service.tasks().list(tasklist=tasklist_id).execute()
    return result.get('items', [])

# Fetch pending tasks
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

# Fetch passed tasks
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

# Display tasks in a pinned message
def display_tasks(service, tasklist_id):
    pending_tasks = get_pending_tasks(service, tasklist_id)
    passed_tasks = get_passed_tasks(service, tasklist_id)

    pending_tasks_md = '\n'.join([f"- **{task['title']}** (Due: {task['due_date']})" for task in pending_tasks])
    passed_tasks_md = '\n'.join([f"- __{task['title']}__ (Was due: {task['due_date']})" for task in passed_tasks])

    message = (
        "### Tasks Overview\n"
        f"**Pending Tasks:**\n{pending_tasks_md if pending_tasks_md else 'No pending tasks.'}\n\n"
        f"**Passed Tasks:**\n{passed_tasks_md if passed_tasks_md else 'No passed tasks.'}\n\n"
    )
    
    return message

# Create a task in Google Tasks
def create_task(task_title, due_date=None, priority=None, description=None):
    print(f"Creating task with title: {task_title}")
    service = authenticate_cloud()

    tasklist_id = get_tasklist_id_by_title(service, "Schule")

    parsed_due_date = None
    if due_date:
        try:
            parsed_due_date = datetime.fromisoformat(due_date).isoformat()
        except ValueError:
            try:
                parsed_due_date = dateutil.parser.parse(due_date).isoformat()
            except ValueError:
                raise ValueError("Invalid due date format.")

    task_body = {'title': task_title}
    if parsed_due_date:
        task_body['due'] = parsed_due_date
    if priority:
        task_body['notes'] = f"Priority: {priority}"
    if description:
        task_body['notes'] = task_body.get('notes', '') + f"\nDescription: {description}"

    task = service.tasks().insert(tasklist=tasklist_id, body=task_body).execute()
    print(f"Task '{task_title}' created with ID: {task['id']}")
    return f"Created task '{task_title}' with ID: {task['id']}"

# Define the input schema for creating a task
class CreateTaskInput(BaseModel):
    task_title: str = Field(description="Title of the task to create")
    due_date: Optional[str] = Field(default=None, description="Due date in RFC3339 or other common date formats, None if no due date")
    priority: Optional[str] = Field(default=None, description="Priority level of the task")
    description: Optional[str] = Field(default=None, description="Description of the task")

# Define tool class for creating a task
class CreateTaskTool(BaseTool):
    name: str = "create_task"
    description: str = "Tool for creating a new task in Google Tasks."
    args_schema: Type[BaseModel] = CreateTaskInput
    return_direct: bool = False

    def _run(
        self, task_title: str, due_date: Optional[str] = None, priority: Optional[str] = None, description: Optional[str] = None, run_manager: Optional = None
    ) -> str:
        return create_task(task_title, due_date, priority, description)
    
class GetCurrentDateInput(BaseModel):
    format: Optional[str] = Field(default="RFC3339", description="Format in which to return the current date (e.g., RFC3339)")

# Define tool for getting the current date
class GetCurrentDateTool(BaseTool):
    name: str = "get_current_date"
    description: str = "Tool for retrieving the current date in the specified format."
    args_schema: Type[BaseModel] = GetCurrentDateInput
    return_direct: bool = False

    def _run(self, format: Optional[str] = "RFC3339", run_manager: Optional = None) -> str:
        current_date = datetime.now(timezone.utc)
        if format == "RFC3339":
            return current_date.isoformat()
        else:
            return current_date.strftime(format)

# Instantiate the tools
create_task_tool = CreateTaskTool()
get_current_date_tool = GetCurrentDateTool()

# Initialize language model agent
llm = ChatOpenAI(
    model_name="gpt-4o-mini",
    base_url="https://models.inference.ai.azure.com",
    api_key=azure_token
)

memory = MemorySaver()

tools = [create_task_tool, get_current_date_tool]
agent_executor = create_react_agent(
    llm, tools, checkpointer=memory, state_modifier="You are an agent that helps students create Google Tasks for their homework..."
)

# Discord bot initialization
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.message_content = True
bot = discord.Client(intents=intents)

# Agent message processing
def agent_send_message(message):
    print(f"Sending message to agent: {message}")
    human_message = HumanMessage(content=message)
    response = agent_executor.invoke(
        {"messages": [human_message]},
        config={"configurable": {"thread_id": "default", "recursion_limit": 1000}},
    )
    return response

# Extract most recent AI message and tool calls
def get_most_recent_ai_message_content_and_tool_calls(response):
    print("Extracting most recent AI message and tool calls...")
    messages = response.get('messages', [])
    most_recent_content = None
    tool_calls = []

    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        if isinstance(message, AIMessage):
            if message.content:
                most_recent_content = message.content
            if 'tool_calls' in message.additional_kwargs:
                tool_calls.extend(message.additional_kwargs.get('tool_calls', []))

    return most_recent_content, tool_calls

@bot.event
async def on_ready():
    global tasklist_id
    print(f'Logged in as {bot.user}')
    tasklist_id = get_tasklist_id_by_title(authenticate_cloud(), "Schule")
    print(f"Tasklist ID set to: {tasklist_id}")
    update_tasks.start()



# On message event
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.channel.name == CHANNEL_NAME:
        content = message.content.strip().lower()

        if content.startswith('/task-history'):
            print("Task history requested.")
            bot_message = await message.channel.send(f"### Last 10 Completed Tasks\n TODO: Implement this feature")
            await asyncio.sleep(10)
            await bot_message.delete()
            await message.delete()
            return

        # Process message with the agent
        response = agent_send_message(message.content)
        agent_message, tool_calls = get_most_recent_ai_message_content_and_tool_calls(response)

        bot_message = await message.channel.send(f"**Agent Response:** {agent_message}")
        await asyncio.sleep(30)  # Optionally delete messages after 30 seconds
        await message.delete()
        await bot_message.delete()

# Updating tasks every 1 minute
@tasks.loop(minutes=1)
async def update_tasks():
    print("Updating tasks...")
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.name == CHANNEL_NAME:
                tasks_overview = display_tasks(authenticate_cloud(), tasklist_id)
                await channel.send(tasks_overview)

# Deleting non-pinned messages every 30 minutes
@tasks.loop(minutes=30)
async def delete_non_pinned_messages():
    print("Deleting non-pinned messages...")
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.name == CHANNEL_NAME:
                async for msg in channel.history(limit=None):
                    if not msg.pinned:
                        await msg.delete()

# Start the bot
async def start_bot():
    await bot.start(TOKEN)

def run_bot():
    asyncio.run(start_bot())

# Run bot and Flask server concurrently
bot_thread = threading.Thread(target=run_bot)
flask_thread = threading.Thread(target=run_flask)
bot_thread.start()
flask_thread.start()
bot_thread.join()
flask_thread.join()
