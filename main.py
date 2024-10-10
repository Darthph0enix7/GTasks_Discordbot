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
from google.oauth2 import service_account
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

german_months = {
    1: "Januar", 2: "Februar", 3: "März", 4: "April", 5: "Mai", 6: "Juni",
    7: "Juli", 8: "August", 9: "September", 10: "Oktober", 11: "November", 12: "Dezember"
}
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
    with open("service_account.json.encrypted", "rb") as encrypted_file:
        encrypted_data = encrypted_file.read()

    fernet = Fernet(encryption_key)
    decrypted_data = fernet.decrypt(encrypted_data)

    with open("service_account.json", "wb") as decrypted_file:
        decrypted_file.write(decrypted_data)
        print("Decrypted token saved to service_account.json")

# Authenticate using OAuth tokens
def authenticate_google_tasks():
    print("Authenticating with Google Tasks API using service account")
    decrypt_token()
    # Load the service account credentials from service_creds.json
    service_account_file = 'service_account.json'
    if not os.path.exists(service_account_file):
        raise Exception(f"Service account file {service_account_file} not found.")
    
    creds = service_account.Credentials.from_service_account_file(service_account_file)
    print("Service account credentials loaded successfully.")
    
    # Return authenticated Google Tasks API service
    return build('tasks', 'v1', credentials=creds)

# Get Task List ID by Task List Title
def get_tasklist_id_by_title(service, title):
    print(f"Fetching tasklist ID for title: {title}")
    result = service.tasklists().list().execute()
    tasklists = result.get('items', [])
    for tasklist in tasklists:
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

    print(f"Total pending tasks: {len(pending_tasks)}")
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
    print(f"Total passed tasks: {len(passed_tasks)}")
    return passed_tasks

def display_tasks(service, tasklist_id):
    print("Displaying tasks")
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
        service = authenticate_google_tasks()

        tasklist_id = get_tasklist_id_by_title(service, "Schule")

        parsed_due_date = None
        if due_date:
            try:
                parsed_due_date = datetime.fromisoformat(due_date).isoformat()
            except ValueError:
                try:
                    parsed_due_date = dateutil.parser.parse(due_date).isoformat()
                except ValueError:
                    raise ValueError("Invalid due date format. Please provide a valid date.")
        task_body = {'title': task_title}
        if parsed_due_date:
            task_body['due'] = parsed_due_date
        if priority:
            task_body['notes'] = f"Priority: {priority}"
        if description:
            task_body['notes'] = (task_body.get('notes', '') + f"\nDescription: {description}").strip()
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
        current_date = datetime.now(timezone.utc)
        if format == "RFC3339":
            date_str = current_date.isoformat()
        else:
            date_str = current_date.strftime(format)
        return date_str

# Instantiate the tools
create_task_tool = CreateTaskTool()
get_current_date_tool = GetCurrentDateTool()


# Fetch all tasks (pending and passed) with their task IDs
def get_pending_and_passed_tasks(service, tasklist_id):
    tasks = get_tasks(service, tasklist_id)
    now = datetime.now(timezone.utc)
    pending_passed_tasks = []

    for task in tasks:
        due_date = task.get('due')
        if task.get('status') != 'completed':  # Only consider non-completed tasks
            task_entry = {
                'title': task['title'],
                'id': task['id'],  # Include the task ID
                'due_date': due_date if due_date else 'No due date'
            }
            pending_passed_tasks.append(task_entry)

    print(f"Retrieved {len(pending_passed_tasks)} pending or passed tasks.")
    return pending_passed_tasks

# Define the custom tool for getting pending and passed tasks with task IDs
class GetPendingAndPassedTasksTool(BaseTool):
    name: str = "get_pending_and_passed_tasks"
    description: str = "Tool to retrieve pending and passed tasks with their task IDs from Google Tasks."
    return_direct: bool = True

    def _run(self, run_manager: Optional = None) -> str:
        """Fetch all pending and passed tasks with task IDs."""
        service = authenticate_google_tasks()

        # Get the tasklist ID
        tasklist_id = get_tasklist_id_by_title(service, "Schule")
        
        # Fetch pending and passed tasks
        tasks = get_pending_and_passed_tasks(service, tasklist_id)

        # Format tasks into a user-friendly output
        task_list_output = "\n".join(
            [f"- Title: {task['title']}, ID: {task['id']}, Due Date: {task['due_date']}" for task in tasks]
        )

        if not task_list_output:
            return "No pending or passed tasks found."

        return f"Pending and Passed Tasks:\n{task_list_output}"
# Mark a task as completed
def mark_task_complete(service, tasklist_id, task_id):
    print(f"Marking task {task_id} as complete in tasklist {tasklist_id}")
    # Set the task status to 'completed'
    task = service.tasks().get(tasklist=tasklist_id, task=task_id).execute()
    task['status'] = 'completed'
    updated_task = service.tasks().update(tasklist=tasklist_id, task=task_id, body=task).execute()
    print(f"Task {task_id} marked as completed.")
    return updated_task

# Mark a task as completed using either its ID or title
def mark_task_complete_by_id_or_title(service, tasklist_id, task_title=None, task_id=None):
    tasks = get_pending_and_passed_tasks(service, tasklist_id)

    # If task ID is provided, find the task directly
    if task_id:
        for task in tasks:
            if task['id'] == task_id:
                return mark_task_complete(service, tasklist_id, task_id)
        return f"Task with ID '{task_id}' not found."

    # If task title is provided, find the task by title
    elif task_title:
        for task in tasks:
            if task['title'].lower() == task_title.lower():
                return mark_task_complete(service, tasklist_id, task['id'])
        return f"Task with title '{task_title}' not found."

    return "Please provide either a task title or task ID."

# Define the input schema for completing (deleting) a task
class CompleteTaskByIdOrTitleInput(BaseModel):
    task_title: Optional[str] = Field(default=None, description="Title of the task to complete (delete)")
    task_id: Optional[str] = Field(default=None, description="ID of the task to complete (delete)")

# Define the custom tool for completing a task in Google Tasks
class CompleteTaskTool(BaseTool):
    name: str = "complete_task"
    description: str = "Tool to mark a task as completed (deleted) by its ID or title."
    args_schema: Type[BaseModel] = CompleteTaskByIdOrTitleInput
    return_direct: bool = False

    def _run(self, task_title: Optional[str] = None, task_id: Optional[str] = None, run_manager: Optional = None) -> str:
        """Mark a task as complete using its title or ID."""
        service = authenticate_google_tasks()

        # Get the tasklist ID for the relevant task list
        tasklist_id = get_tasklist_id_by_title(service, "Schule")

        # Mark the task as completed by ID or title
        result = mark_task_complete_by_id_or_title(service, tasklist_id, task_title=task_title, task_id=task_id)
        
        return result

# Instantiate the tool
complete_task_tool = CompleteTaskTool()
# Instantiate the tool
get_pending_tasks_tool = GetPendingAndPassedTasksTool()


# Mycroft agent configuration
model_name = "gpt-4o-mini"
endpoint = "https://models.inference.ai.azure.com"
system_prompt = """You are an MYcroft-mini a assistant that helps students create Google Tasks for their homework. Process each assignment, 
create a corresponding task with all provided details, and fit short info into the title. 
Determine exact due dates from relative terms using the current date in RFC3339 format None if no due date is given. 
Debug and retry if issues arise. Always use the same language as the user."""

# Initialize the language model
llm = ChatOpenAI(
    model_name=model_name,
    base_url=endpoint,
    api_key=azure_token
)

# Initialize memory saver
memory = MemorySaver()

# Create the agent executor
tools = [get_current_date_tool, create_task_tool, complete_task_tool, get_pending_tasks_tool]
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
    update_tasks.start()  # Start updating tasks every minute

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.channel.name == CHANNEL_NAME:
        content = message.content.strip().lower()
        print(f"Received message: {content}")

        if content.startswith('/task-history'):
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
        bot_message = await message.channel.send(f"**Agent Response:** {agent_message}")
        await asyncio.sleep(30)  # Optionally delete messages after 30 seconds
        await message.delete()
        await bot_message.delete()

@tasks.loop(seconds=10)  # Loop to update tasks every 10 seconds
async def update_tasks():
    global pinned_message_id
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
