#!/usr/bin/env python3
"""
gateway/app.py — FastAPI bridge: n8n (HTTP) -> ROS 2 (rosbridge WebSocket) -> NAO

Endpoints:
    POST /nao/move      — velocity command (geometry_msgs/Twist)
    POST /nao/say       — text-to-speech
    POST /nao/gesture   — posture (Stand / Sit / StandZero / Crouch / LyingBack)
    POST /nao/leds      — LED colour (name + r/g/b)
    POST /nao/stop      — emergency stop
    GET  /nao/status    — one-shot status read from robot (battery, temp)
    GET  /health        — liveness probe
"""

import asyncio
import json
import os
from typing import Optional

import websockets
from fastapi import FastAPI, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel, Field

ROSBRIDGE_URL = os.environ.get("ROSBRIDGE_URL", "ws://ros2-bridge:9090")
API_KEY       = os.environ.get("GATEWAY_API_KEY", "CHANGE_THIS_GATEWAY_KEY")
WS_TIMEOUT    = 5.0   # seconds — connection + send timeout

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class MoveCmd(BaseModel):
    linear_x:  float = Field(0.0, ge=-1.0, le=1.0)
    linear_y:  float = Field(0.0, ge=-1.0, le=1.0)
    angular_z: float = Field(0.0, ge=-1.0, le=1.0)

class SpeechCmd(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)

class GestureCmd(BaseModel):
    posture: str  # Stand | Sit | StandZero | Crouch | LyingBack | LyingBelly

class LedCmd(BaseModel):
    name:     str   = "FaceLeds"
    r:        float = Field(1.0, ge=0.0, le=1.0)
    g:        float = Field(1.0, ge=0.0, le=1.0)
    b:        float = Field(0.0, ge=0.0, le=1.0)
    duration: float = Field(0.5, ge=0.0, le=5.0)


# ---------------------------------------------------------------------------
# rosbridge helpers
# ---------------------------------------------------------------------------

async def _ros_publish(topic: str, msg_type: str, msg: dict) -> None:
    try:
        async with websockets.connect(ROSBRIDGE_URL, open_timeout=WS_TIMEOUT) as ws:
            await asyncio.wait_for(
                ws.send(json.dumps({"op": "publish", "topic": topic, "msg": msg})),
                timeout=WS_TIMEOUT,
            )
    except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as exc:
        raise HTTPException(status_code=503, detail=f"rosbridge unavailable: {exc}")


async def _ros_read_once(topic: str, timeout: float = 5.0) -> dict:
    """Subscribe, read one message, unsubscribe."""
    try:
        async with websockets.connect(ROSBRIDGE_URL, open_timeout=WS_TIMEOUT) as ws:
            await ws.send(json.dumps({"op": "subscribe", "topic": topic}))
            raw  = await asyncio.wait_for(ws.recv(), timeout=timeout)
            data = json.loads(raw)
            await ws.send(json.dumps({"op": "unsubscribe", "topic": topic}))
            return data.get("msg", {})
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="NAO status timeout — robot may be offline")
    except (OSError, websockets.WebSocketException) as exc:
        raise HTTPException(status_code=503, detail=f"rosbridge unavailable: {exc}")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def _verify(key: str = Security(api_key_header)) -> str:
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return key


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="NAO Gateway", version="1.0.0", docs_url="/docs")


@app.get("/health", tags=["infra"])
async def health():
    return {"status": "ok", "rosbridge": ROSBRIDGE_URL}


@app.post("/nao/move", tags=["nao"])
async def move(cmd: MoveCmd, _: str = Security(_verify)):
    msg = {
        "linear":  {"x": cmd.linear_x, "y": cmd.linear_y, "z": 0.0},
        "angular": {"x": 0.0,          "y": 0.0,          "z": cmd.angular_z},
    }
    await _ros_publish("/nao/cmd_vel", "geometry_msgs/Twist", msg)
    return {"ok": True}


@app.post("/nao/say", tags=["nao"])
async def say(cmd: SpeechCmd, _: str = Security(_verify)):
    await _ros_publish("/nao/speech", "std_msgs/String", {"data": cmd.text})
    return {"ok": True}


@app.post("/nao/gesture", tags=["nao"])
async def gesture(cmd: GestureCmd, _: str = Security(_verify)):
    await _ros_publish("/nao/gesture", "std_msgs/String", {"data": cmd.posture})
    return {"ok": True}


@app.post("/nao/leds", tags=["nao"])
async def leds(cmd: LedCmd, _: str = Security(_verify)):
    payload = json.dumps({
        "name": cmd.name, "r": cmd.r, "g": cmd.g, "b": cmd.b, "duration": cmd.duration,
    })
    await _ros_publish("/nao/leds", "std_msgs/String", {"data": payload})
    return {"ok": True}


@app.post("/nao/stop", tags=["nao"])
async def stop(_: str = Security(_verify)):
    await _ros_publish("/nao/stop", "std_msgs/Empty", {})
    return {"ok": True}


@app.get("/nao/status", tags=["nao"])
async def status(_: str = Security(_verify)):
    msg  = await _ros_read_once("/nao/status")
    data = json.loads(msg.get("data", "{}"))
    return {"ok": True, "status": data}
