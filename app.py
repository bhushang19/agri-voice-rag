import os
import json
import base64
import asyncio
import websockets
import logging
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from dotenv import load_dotenv

load_dotenv()

# Configuration
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_API_ENDPOINT = os.getenv("AZURE_OPENAI_API_ENDPOINT")
AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
AZURE_SEARCH_KEY = os.getenv("AZURE_SEARCH_KEY")
AZURE_SEARCH_INDEX = os.getenv("AZURE_SEARCH_INDEX")
AZURE_SEARCH_SEMANTIC_CONFIGURATION = os.getenv("AZURE_SEARCH_SEMANTIC_CONFIGURATION")
PORT = int(os.getenv("PORT", 5050))
VOICE = "alloy"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Azure Search Client Setup
credential = AzureKeyCredential(AZURE_SEARCH_KEY)
search_client = SearchClient(
    endpoint=AZURE_SEARCH_ENDPOINT, 
    index_name=AZURE_SEARCH_INDEX, 
    credential=credential
)

# FastAPI App
app = FastAPI()


@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Application is running!"}


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    response = VoiceResponse()
    response.say("Please wait while we connect your call.")
    response.pause(length=1)
    response.say("You can start talking now!")
    host = request.url.hostname
    connect = Connect()
    connect.stream(url=f"wss://{host}/media-stream")
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")


@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    logger.info("WebSocket connection opened.")
    await websocket.accept()

    stream_sid = None

    # Create extra headers for Azure OpenAI
    extra_headers = {"api-key": AZURE_OPENAI_API_KEY}
    async with websockets.connect(
        AZURE_OPENAI_API_ENDPOINT,
        extra_headers=extra_headers
    ) as openai_ws:
        await initialize_session(openai_ws)

        async def receive_from_twilio():
            nonlocal stream_sid
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data["event"] == "media":
                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": data["media"]["payload"],
                        }
                        await openai_ws.send(json.dumps(audio_append))
                    elif data["event"] == "start":
                        stream_sid = data["start"]["streamSid"]
                        logger.info(f"Stream started with SID: {stream_sid}")
            except WebSocketDisconnect:
                logger.warning("WebSocket disconnected by client.")
                if openai_ws.open:
                    await openai_ws.close()

        async def send_to_twilio():
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)

                    # Log OpenAI responses
                    # logger.info(f"OpenAI Response: {response}")

                    if response.get("type") == "response.audio.delta" and "delta" in response:
                        audio_payload = base64.b64encode(
                            base64.b64decode(response["delta"])
                        ).decode("utf-8")
                        audio_delta = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": audio_payload},
                        }
                        await websocket.send_json(audio_delta)

                    # Handle function calls for RAG
                    if response.get("type") == "response.function_call_arguments.done":
                        function_name = response["name"]
                        if function_name == "get_additional_context":
                            query = json.loads(response["arguments"]).get("query", "")
                            search_results = azure_search_rag(query)
                            logger.info(f"RAG Results: {search_results}")
                            await send_function_output(openai_ws, response["call_id"], search_results)

                    # Trigger RAG search when input is committed
                    if response.get("type") == "input_audio_buffer.committed":
                        query = response.get("text", "").strip()
                        # if query:
                        #     logger.info(f"Triggering RAG search for query: {query}")
                        #     await trigger_rag_search(openai_ws, query)
                        # else:
                        #     logger.warning("Received empty query; skipping RAG search.")
            except Exception as e:
                logger.error(f"Error in send_to_twilio: {e}")

        await asyncio.gather(receive_from_twilio(), send_to_twilio())


async def initialize_session(openai_ws):
    """Initialize the OpenAI session with instructions and tools."""
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": (
                "You are Kisan Mitra, a helpful agricultural assistant for farmers. "
                "Always start with 'Namaste, main Kisan Mitra hoon. Main aapki kya madad kar sakti hoon?' "
                "You must first detect the language of the farmer's input. Here's how to handle languages: "
                "1. If the farmer speaks in any Indian language (Hindi, Marathi, Punjabi, Bengali, Tamil, Telugu, etc.), "
                    "   try to understand and respond in the SAME language if you are capable. "
                "2. If you cannot confidently respond in the detected language, politely inform them in Hindi: "
                "   'Mujhe khed hai, main [detected_language] mein baat nahi kar sakti. Kya main Hindi mein aapki madad kar sakti hoon?' "
                "3. For English queries, respond in English but offer to switch to Hindi if preferred. "
                "4. Always maintain a respectful tone using appropriate honorifics in each language. "
                "\n"
                "When providing agricultural information: "
                "1. Keep responses simple and practical for farmers "
                "2. Use basic terms that farmers can easily understand "
                "3. For complex farming techniques, break down the explanation into simple steps "
                "4. If you don't find specific information in the search results, clearly state that "
                "5. Always verify information from the search results before providing advice "
                "6.""End conversations appropriately in the detected language. For example: "
                    "- Hindi: 'Dhanyavaad. Kya main aapki aur koi madad kar sakti hoon?' "
                    "- Marathi: 'Dhanyavaad. Mi aapli aadhik kahi madad karu shakto ka?' "
                    "- Punjabi: 'Dhanyavaad. Ki mai tuhaḍī hora madad kar sakdī hāṁ?' "
                    "- English: 'Thank you. Can I help you with anything else?' "
                "If unsure about ending in a specific language, default to Hindi."
            ),
            "tools": [
                {
                    "type": "function",
                    "name": "get_additional_context",
                    "description": "Fetch context from Azure Search based on a user query.",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                }
            ],
        },
    }
    await openai_ws.send(json.dumps(session_update))


async def trigger_rag_search(openai_ws, query):
    """Trigger RAG search for a specific query."""
    search_function_call = {
        "type": "conversation.item.create",
        "item": {
            "type": "function_call",
            "name": "get_additional_context",
            "arguments": {"query": query},
        },
    }
    await openai_ws.send(json.dumps(search_function_call))


async def send_function_output(openai_ws, call_id, output):
    """Send RAG results back to OpenAI."""
    response = {
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
        },
    }
    await openai_ws.send(json.dumps(response))

    # Prompt OpenAI to continue processing
    await openai_ws.send(json.dumps({"type": "response.create"}))


def azure_search_rag(query):
    """Perform Azure Cognitive Search and return results."""
    try:
        logger.info(f"Querying Azure Search with: {query}")
        results = search_client.search(
            search_text=query,
            top=2,
            query_type="semantic",
            semantic_configuration_name=AZURE_SEARCH_SEMANTIC_CONFIGURATION,
        )
        summarized_results = [doc.get("chunk", "No content available") for doc in results]
        if not summarized_results:
            return "No relevant information found in Azure Search."
        return "\n".join(summarized_results)
    except Exception as e:
        logger.error(f"Error in Azure Search: {e}")
        return "Error retrieving data from Azure Search."


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)