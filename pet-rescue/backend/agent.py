import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3
from dotenv import load_dotenv

load_dotenv()

from backend.prompts import build_system_prompt
from backend.tools import (
    find_active_rescuers,
    find_emergency_vet,
    get_protocol,
    search_animals,
)

logger = logging.getLogger(__name__)

_KOLKATA = ZoneInfo("Asia/Kolkata")

_sessions: dict[str, list[dict]] = {}

_bedrock_client = boto3.client(
    "bedrock-runtime",
    region_name=os.environ["AWS_REGION"],
)

TOOL_CONFIG = {
    "tools": [
        {
            "toolSpec": {
                "name": "search_animals",
                "description": (
                    "Hybrid (BM25 + semantic) search across all available animals. "
                    "Use for adoption queries. Returns up to top_k matching animals."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Natural-language description of what the user wants.",
                            },
                            "species": {
                                "type": "string",
                                "description": "'dog', 'cat', etc. Optional.",
                            },
                            "size": {
                                "type": "string",
                                "description": "'small', 'medium', or 'large'. Optional.",
                            },
                            "max_age_months": {
                                "type": "integer",
                                "description": "Max age in months. Optional.",
                            },
                            "good_with_kids": {
                                "type": "boolean",
                                "description": "Only return pets explicitly good with kids. Optional.",
                            },
                            "good_with_dogs": {
                                "type": "boolean",
                                "description": "Only return pets explicitly good with dogs. Optional.",
                            },
                            "top_k": {
                                "type": "integer",
                                "description": "Max results to return. Defaults to 5.",
                            },
                        },
                        "required": ["query"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "find_emergency_vet",
                "description": (
                    "Find the nearest 24x7 emergency-capable vets open right now, "
                    "sorted by distance from the given coordinates."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "lat": {
                                "type": "number",
                                "description": "Latitude of the incident location.",
                            },
                            "lon": {
                                "type": "number",
                                "description": "Longitude of the incident location.",
                            },
                            "radius_km": {
                                "type": "integer",
                                "description": "Search radius in km. Defaults to 5.",
                            },
                        },
                        "required": ["lat", "lon"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "find_active_rescuers",
                "description": (
                    "Find on-call animal rescuers covering a specific area at a given time. "
                    "Falls back to wider Bangalore rescuers if no exact area match, "
                    "flagging results with area_match=False."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "area": {
                                "type": "string",
                                "description": "Neighbourhood or area name, e.g. 'Koramangala'.",
                            },
                            "time_iso": {
                                "type": "string",
                                "description": "ISO 8601 timestamp for when the rescue is needed.",
                            },
                        },
                        "required": ["area", "time_iso"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "get_protocol",
                "description": (
                    "Retrieve the most relevant first-aid protocol for an emergency scenario "
                    "using semantic search."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "scenario": {
                                "type": "string",
                                "description": "Description of the emergency, e.g. 'dog hit by vehicle'.",
                            },
                        },
                        "required": ["scenario"],
                    }
                },
            }
        },
    ]
}


def _execute_tool(name: str, input_args: dict, tool_use_id: str, structured: dict) -> dict:
    try:
        if name == "search_animals":
            result = search_animals(**input_args)
            structured["animals"].extend(result)
        elif name == "find_emergency_vet":
            result = find_emergency_vet(**input_args)
            structured["vets"].extend(result)
        elif name == "find_active_rescuers":
            result = find_active_rescuers(**input_args)
            structured["rescuers"].extend(result)
        elif name == "get_protocol":
            result = get_protocol(**input_args)
            if result is not None:
                structured["protocols"].append(result)
        else:
            raise ValueError(f"Unknown tool: {name}")

        # Bedrock requires toolResult content json to be an object, not an array.
        if isinstance(result, list):
            payload = {"results": result}
        elif result is None:
            payload = {"results": []}
        else:
            payload = result

        return {
            "toolResult": {
                "toolUseId": tool_use_id,
                "content": [{"json": payload}],
                "status": "success",
            }
        }
    except Exception:
        logger.exception("Tool %s failed with args %s", name, input_args)
        return {
            "toolResult": {
                "toolUseId": tool_use_id,
                "content": [{"text": f"Tool execution failed for {name}."}],
                "status": "error",
            }
        }


def run_agent(
    message: str,
    session_id: str,
    channel: str = "web",
    user_location: dict | None = None,
) -> tuple[str, dict]:
    structured_results: dict = {"animals": [], "vets": [], "rescuers": [], "protocols": []}
    history = _sessions.setdefault(session_id, [])
    history.append({"role": "user", "content": [{"text": message}]})

    if user_location:
        logger.info("session=%s user_location=%s", session_id, user_location)

    now_ist = datetime.now(_KOLKATA).isoformat()
    system_prompt = build_system_prompt(
        channel=channel,
        current_time=now_ist,
        user_location=user_location,
    )
    model_id = os.environ["BEDROCK_MODEL_ID"]

    for _ in range(5):
        resp = _bedrock_client.converse(
            modelId=model_id,
            messages=history,
            system=[{"text": system_prompt}],
            toolConfig=TOOL_CONFIG,
            inferenceConfig={"maxTokens": 2048},
        )

        assistant_message = resp["output"]["message"]
        history.append(assistant_message)

        stop_reason = resp.get("stopReason")
        if stop_reason != "tool_use":
            text_parts = [
                block["text"]
                for block in assistant_message.get("content", [])
                if "text" in block
            ]
            return "\n".join(text_parts), structured_results

        tool_result_blocks = []
        for block in assistant_message.get("content", []):
            if "toolUse" not in block:
                continue
            tool_use = block["toolUse"]
            result_block = _execute_tool(
                name=tool_use["name"],
                input_args=tool_use["input"],
                tool_use_id=tool_use["toolUseId"],
                structured=structured_results,
            )
            tool_result_blocks.append(result_block)

        history.append({"role": "user", "content": tool_result_blocks})

    return "I couldn't complete that — please try again.", structured_results
