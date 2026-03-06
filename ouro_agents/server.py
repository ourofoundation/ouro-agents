from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import Optional, Any, Dict
from datetime import datetime
from uuid import uuid4
import uvicorn
import asyncio

from .config import OuroAgentsConfig, RunMode
from .agent import OuroAgent

app = FastAPI(title="Ouro Agents Server")

# Global state
agent_instance: Optional[OuroAgent] = None
last_heartbeat: Optional[datetime] = None
start_time: datetime = datetime.utcnow()
session_threads: Dict[str, str] = {}

class RunRequest(BaseModel):
    task: str
    conversation_id: Optional[str] = None
    session_id: Optional[str] = None
    mode: Optional[str] = None
    user_id: Optional[str] = None

class EventPayload(BaseModel):
    event_type: str
    data: Dict[str, Any]

@app.on_event("startup")
async def startup_event():
    global agent_instance
    config = OuroAgentsConfig.load_from_file("config.json")
    agent_instance = OuroAgent(config)
    agent_instance.connect_mcp()
    
    if config.heartbeat.enabled:
        from .heartbeat import start_scheduler
        start_scheduler(agent_instance, config.heartbeat)

@app.on_event("shutdown")
async def shutdown_event():
    if agent_instance:
        agent_instance.close()

@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "uptime_seconds": (datetime.utcnow() - start_time).total_seconds(),
        "last_heartbeat": last_heartbeat.isoformat() if last_heartbeat else None,
        "agent_name": agent_instance.config.agent.name if agent_instance else None
    }

@app.post("/run")
async def run_task(request: RunRequest):
    if not agent_instance:
        raise HTTPException(status_code=503, detail="Agent not initialized")
        
    try:
        conversation_id = request.conversation_id
        if not conversation_id:
            if request.session_id and request.session_id in session_threads:
                conversation_id = session_threads[request.session_id]
            else:
                conversation_id = str(uuid4())
                if request.session_id:
                    session_threads[request.session_id] = conversation_id

        mode = RunMode(request.mode) if request.mode else RunMode.AUTONOMOUS
        result = await agent_instance.run(
            task=request.task,
            conversation_id=conversation_id,
            mode=mode,
            user_id=request.user_id,
        )
        return {
            "status": "success",
            "result": result,
            "conversation_id": conversation_id,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/events")
async def handle_event(payload: EventPayload, background_tasks: BackgroundTasks):
    """Webhook receiver for Ouro platform events."""
    if not agent_instance:
        raise HTTPException(status_code=503, detail="Agent not initialized")
        
    # Extract relevant info from event
    event_type = payload.event_type
    data = payload.data
    
    conversation_id = data.get("conversation_id")
    user_id = data.get("sender_username") or data.get("sender")

    if event_type == "new_message":
        content = data.get("content", "")
        sender = data.get("sender", "Unknown")
        task = f"Message from {sender}:\n\n{content}"
        mode = RunMode.CHAT
    else:
        task = f"Received event: {event_type}\nData: {data}\n\nDetermine if any action is needed."
        mode = RunMode.AUTONOMOUS

    background_tasks.add_task(
        agent_instance.run,
        task=task,
        conversation_id=conversation_id,
        mode=mode,
        user_id=user_id,
    )
    
    return {"status": "accepted", "event_type": event_type}

def start_server(config_path: str = "config.json"):
    config = OuroAgentsConfig.load_from_file(config_path)
    uvicorn.run(
        "ouro_agents.server:app",
        host=config.server.host,
        port=config.server.port,
        reload=False
    )
