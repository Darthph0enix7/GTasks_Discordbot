import os
import time
import pickle
import asyncio
from datetime import datetime, timezone
from typing import Optional, Type
from cryptography.fernet import Fernet
from flask import Flask, request, jsonify
import threading
import pickle

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

app = Flask(__name__)

@app.route('/')
def health_check():
    return "Health Check OK", 200

auth_code = None

@app.route('/auth', methods=['POST'])
def receive_auth_code():
    global auth_code
    auth_code = request.json.get('code')
    return jsonify({"message": "Authorization code received"}), 200

def run_flask():
    app.run(host='0.0.0.0', port=8000)

# Scopes allow us to read and write tasks
SCOPES = ['https://www.googleapis.com/auth/tasks']
load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
CHANNEL_NAME = 'hausaufgaben'
azure_token = os.getenv("AZURE_TOKEN")
key = os.environ.get('ENCRYPTION_KEY').encode()
auth_code = None

# Load the encrypted file
with open("client_secret.json.encrypted", "rb") as file:
    encrypted_data = file.read()

# Decrypt the file
fernet = Fernet(key)
decrypted_data = fernet.decrypt(encrypted_data)

# Save the decrypted file
with open("client_secret.json", "wb") as file:
    file.write(decrypted_data)

def authenticate_google_tasks():
    global auth_code
    creds = None
    # The token.pickle stores the user's access and refresh tokens
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)

    # If credentials are not available or expired, get new ones
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'client_secret.json', SCOPES,
                redirect_uri='urn:ietf:wg:oauth:2.0:oob'
            )
            auth_url, _ = flow.authorization_url()
            print(f"Please go to this URL: {auth_url}")

            # Wait for the authorization code to be received via the Flask route
            while auth_code is None:
                pass

            creds = flow.fetch_token(code=auth_code)

        # Save the credentials for the next run
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)

    return build('tasks', 'v1', credentials=creds)

# Get Task List ID by Task List Title
def get_tasklist_id_by_title(service, title):
    result = service.tasklists().list().execute()
    tasklists = result.get('items', [])
    for tasklist in tasklists:
        if tasklist['title'].lower() == title.lower():
            return tasklist['id']
    raise ValueError(f"Tasklist '{title}' not found")

# Fetch all tasks from the tasklist
def get_tasks(service, tasklist_id):
    result = service.tasks().list(tasklist=tasklist_id).execute()
    return result.get('items', [])

# Fetch pending tasks (tasks that are not completed)
def get_pending_tasks(service, tasklist_id):
    tasks = get_tasks(service, tasklist_id)
    pending_tasks = []
    now = datetime.now(timezone.utc)

    for task in tasks:
        due_date = task.get('due')
        if task.get('status') != 'completed':  # Only display tasks that are not completed
            if due_date:
                due_datetime = datetime.fromisoformat(due_date[:-1] + '+00:00')
                if due_datetime > now:
                    pending_tasks.append({
                        'title': task['title'],
                        'due_date': due_datetime.strftime('%Y-%m-%d')
                    })
            else:
                # Include tasks without a due date in pending tasks
                pending_tasks.append({
                    'title': task['title'],
                    'due_date': 'No due date'
                })

    return pending_tasks

# Fetch passed tasks (tasks where the due date has passed and they are not marked as completed)
def get_passed_tasks(service, tasklist_id):
    tasks = get_tasks(service, tasklist_id)
    passed_tasks = []
    now = datetime.now(timezone.utc)

    for task in tasks:
        due_date = task.get('due')
        if task.get('status') != 'completed':  # Only consider non-completed tasks
            if due_date:
                due_datetime = datetime.fromisoformat(due_date[:-1] + '+00:00')
                if due_datetime < now:
                    passed_tasks.append({
                        'title': task['title'],
                        'due_date': due_datetime.strftime('%Y-%m-%d')
                    })

    return passed_tasks

# Example to display the tasks in a pinned message
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
        # Authenticate and get the Google Tasks service
        service = authenticate_google_tasks()

        # Get the task list ID
        tasklist_id = get_tasklist_id_by_title(service, "Schule")

        # Parse the due date if provided
        parsed_due_date = None
        if due_date:
            try:
                parsed_due_date = datetime.fromisoformat(due_date).isoformat()
            except ValueError:
                try:
                    parsed_due_date = dateutil.parser.parse(due_date).isoformat()
                except ValueError:
                    raise ValueError("Invalid due date format. Please provide a valid date.")

        # Prepare the task body
        task_body = {'title': task_title}
        if parsed_due_date:
            task_body['due'] = parsed_due_date
        if priority:
            task_body['notes'] = f"Priority: {priority}"
        if description:
            task_body['notes'] = (task_body['notes'] + f"\nDescription: {description}") if 'notes' in task_body else f"Description: {description}"

        # Create the task
        task = service.tasks().insert(tasklist=tasklist_id, body=task_body).execute()
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
        current_date = datetime.now(timezone.utc)
        if format == "RFC3339":
            return current_date.isoformat()
        else:
            return current_date.strftime(format)

# Instantiate the tools
create_task_tool = CreateTaskTool()
get_current_date_tool = GetCurrentDateTool()

# Mycroft agent configuration
model_name = "gpt-4o-mini"
endpoint = "https://models.inference.ai.azure.com"
system_prompt = """You are an agent that helps students create Google Tasks for their homework. Process each assignment, create a corresponding task with all provided details, and fit short info into the title. Determine exact due dates from relative terms using the current date in RFC3339 format None if no due date is given. Debug and retry if issues arise. Always use the same language as the user."""

# Initialize the language model
llm = ChatOpenAI(
    model_name=model_name,
    base_url=endpoint,
    api_key=azure_token
)

# Initialize memory saver
memory = MemorySaver()

# Create the agent executor
tools = [get_current_date_tool, create_task_tool]
agent_executor = create_react_agent(
    llm, tools, checkpointer=memory, state_modifier=system_prompt
)

# Create and start the Flask thread
flask_thread = threading.Thread(target=run_flask)
flask_thread.start()

# Wait for a short period to ensure the Flask server is running
time.sleep(2)

# Initialize the Discord Bot
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.message_content = True

bot = discord.Client(intents=intents)

# Authenticate Google Tasks
service = authenticate_google_tasks()


# Example function to send a message to the agent
def agent_send_message(message):
    human_message = HumanMessage(content=message)
    response = agent_executor.invoke(
        {"messages": [human_message]},
        config={"configurable": {"thread_id": "default", "recursion_limit": 1000}},
    )
    return response

# Function to extract the most recent message content and tool calls from the agent's response
def get_most_recent_ai_message_content_and_tool_calls(response):
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
    
    tasklist_id = get_tasklist_id_by_title(service, "Schule")  # Set the task list ID
    update_tasks.start()  # Start updating tasks every minute


async def clear_channel():
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.name == CHANNEL_NAME:
                async for msg in channel.history(limit=None):
                    await msg.delete()

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.channel.name == CHANNEL_NAME:
        content = message.content.strip().lower()

        if content.startswith('/task-history'):
            bot_message = await message.channel.send(f"### Last 10 Completed Tasks\n TODO: Implement this feature")
            await asyncio.sleep(10)
            await bot_message.delete()
            await message.delete()
            return

        # Pass the user message to the agent
        response = agent_send_message(message.content)
        agent_message, tool_calls = get_most_recent_ai_message_content_and_tool_calls(response)

        # Send agent response back to the Discord channel
        bot_message = await message.channel.send(f"**Agent Response:** {agent_message}")
        await asyncio.sleep(30)  # Optionally delete messages after 30 seconds
        await message.delete()
        await bot_message.delete()

@tasks.loop(minutes=1)  # Loop to update tasks every 1 minute
async def update_tasks():
    global pinned_message_id
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.name == CHANNEL_NAME:
                # Fetch the current pinned message
                if pinned_message_id is None:
                    async for msg in channel.history(limit=10):
                        if msg.pinned and msg.author == bot.user and msg.content.startswith('### Tasks Overview'):
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


@tasks.loop(minutes=30)
async def delete_non_pinned_messages():
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.name == CHANNEL_NAME:
                async for msg in channel.history(limit=None):
                    if not msg.pinned and not msg.content.startswith('### Pinned Tasks'):
                        await msg.delete()

async def start_bot():
    await bot.start(TOKEN)

def run_bot():
    asyncio.run(start_bot())


# Create and start the bot thread
bot_thread = threading.Thread(target=run_bot)
bot_thread.start()

# Join the threads to ensure they run concurrently
bot_thread.join()
flask_thread.join()

tasklist_id = None
pinned_message_id = None 