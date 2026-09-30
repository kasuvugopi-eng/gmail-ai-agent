"""Automated Email Triage and Processing Agent.

This module connects to the Gmail API to retrieve unread emails, uses a Google
Gemini LLM via LangChain and LangGraph to classify messages (Promotional, Routine,
or Sensitive), creates drafts or alerts as appropriate, and sends status reports
to a designated Telegram chat.
"""

import base64
from email.message import EmailMessage
import json
import os
import time
from typing import Any, Dict, List, Literal, Optional, Set, TypedDict, Union

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
import requests

load_dotenv()

# --- 1. CONFIGURATION & LOG TRACKING ---
SCOPES: List[str] = ["https://mail.google.com/"]
LOG_FILE: str = "processed_ids.json"
PENDING_TRASH_FILE: str = "pending_trash.json"

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "").strip().strip('"').strip("'")

if TELEGRAM_BOT_TOKEN.startswith("bot"):
    TELEGRAM_BOT_TOKEN = TELEGRAM_BOT_TOKEN[3:]


def load_processed_ids() -> Set[str]:
    """Loads previously processed email IDs from local JSON storage.

    Returns:
        Set[str]: A set of processed email IDs. Returns an empty set if the file
            does not exist or encounters a decoding error.
    """
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_processed_id(email_id: str) -> None:
    """Persists a newly processed email ID to local JSON storage.

    Args:
        email_id: Unique identifier string of the processed email.
    """
    ids = load_processed_ids()
    ids.add(email_id)
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(list(ids), f)


def load_pending_trash() -> List[Union[Dict[str, str], str]]:
    """Loads emails queued for promotional trash deletion.

    Returns:
        List[Union[Dict[str, str], str]]: A list of promotional email records
            or IDs pending deletion review.
    """
    if os.path.exists(PENDING_TRASH_FILE):
        try:
            with open(PENDING_TRASH_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_pending_trash(trash_list: List[Union[Dict[str, str], str]]) -> None:
    """Saves the pending promotional trash list to local storage.

    Args:
        trash_list: A list containing email metadata dictionaries or email IDs.
    """
    with open(PENDING_TRASH_FILE, "w", encoding="utf-8") as f:
        json.dump(trash_list, f, indent=2)


# --- 2. DATA SCHEMAS ---
class EmailAnalysis(BaseModel):
    """Pydantic model representing structured email classification output."""

    category: Literal["PROMOTIONAL", "ROUTINE", "SENSITIVE"] = Field(
        description="The classification category of the email."
    )
    priority: Literal["LOW", "MEDIUM", "HIGH"] = Field(
        description="The priority level of the email."
    )
    summary: str = Field(
        description="A crisp 1-2 sentence summary of what the email is about."
    )


class EmailState(TypedDict):
    """Typed dictionary representing the shared state across the LangGraph pipeline."""

    email_id: str
    thread_id: str
    sender: str
    subject: str
    body: str
    analysis: Optional[EmailAnalysis]
    generated_reply: Optional[str]


# --- 3. TELEGRAM UTILITY FUNCTIONS ---
def send_telegram_alert(text: str) -> None:
    """Dispatches a plain text alert notification to Telegram.

    Args:
        text: Message body to be dispatched to the chat.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram alert error: {e}")


def send_telegram_draft_review(
    sender: str, subject: str, draft_body: str, draft_id: str
) -> None:
    """Sends draft reply details to Telegram with interactive action buttons.

    Args:
        sender: The email address of the original sender.
        subject: The subject of the thread.
        draft_body: The LLM-generated draft reply text.
        draft_id: The Gmail draft ID used for inline button callback actions.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    message_text = (
        f"📝 NEW DRAFT CREATED IN GMAIL\n\n"
        f"To: {sender}\n"
        f"Subject: {subject}\n\n"
        f"Draft Reply:\n{draft_body}"
    )
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message_text,
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": "✅ Send Reply", "callback_data": f"send:{draft_id}"},
                    {"text": "❌ Keep Draft", "callback_data": f"keep:{draft_id}"},
                ]
            ]
        },
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram draft notification error: {e}")


def send_telegram_trash_summary(all_trash_list: List[Union[Dict[str, str], str]]) -> Optional[Dict[str, Any]]:
    """Sends a consolidated preview and bulk delete action for promotional emails.

    Displays up to the first 15 queued emails with a markdown list and appends
    an inline keyboard button allowing bulk deletion.

    Args:
        all_trash_list: List of queued promotional email dictionaries or IDs.

    Returns:
        Optional[Dict[str, Any]]: Telegram API response dictionary if successful,
            or None if the list is empty or an error occurs.
    """
    if not all_trash_list:
        return None

    count = len(all_trash_list)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    text = f"🗑️ *PENDING PROMOTIONAL EMAILS ({count} total)*\n\n"

    # Display the first 15 emails as a preview
    for idx, item in enumerate(all_trash_list[:15], 1):
        if isinstance(item, dict):
            clean_subj = item.get("subject", "No Subject").replace("*", "").replace("_", "")
        else:
            clean_subj = f"Email ID: {item}"
        text += f"{idx}. *{clean_subj}*\n"

    if count > 15:
        text += f"\n...and *{count - 15} more* promotional emails."

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [
                [
                    {
                        "text": f"🗑 Delete All {count} Promotional Mails",
                        "callback_data": "trash_all",
                    }
                ]
            ]
        },
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        return res.json()
    except Exception as e:
        print(f"Telegram trash summary error: {e}")
        return None


# --- 4. GMAIL AUTHENTICATION & HELPERS ---
def get_gmail_service() -> Resource:
    """Authenticates using OAuth 2.0 and constructs the Gmail API service client.

    Refreshes expired credentials if a refresh token is present, or initiates a
    local server flow using client secrets if no valid token exists.

    Returns:
        Resource: Authorized Google API client resource for Gmail v1.
    """
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open("token.json", "w", encoding="utf-8") as token:
            token.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


gmail_service = get_gmail_service()


def create_raw_email(to: str, subject: str, message_text: Any) -> str:
    """Encodes an email message into a URL-safe base64 string for Gmail API.

    Args:
        to: Target recipient email address.
        subject: Subject header of the email.
        message_text: Email body text (string or list of content segments).

    Returns:
        str: URL-safe base64-encoded email payload.
    """
    message = EmailMessage()
    if isinstance(message_text, list):
        clean_text = "\n".join(
            part.get("text", str(part)) if isinstance(part, dict) else str(part)
            for part in message_text
        )
    else:
        clean_text = str(message_text)

    message.set_content(clean_text)
    message["To"] = to
    message["Subject"] = subject
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


# --- 5. LLM SETUP ---
llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash")
classifier_llm = llm.with_structured_output(EmailAnalysis)

classify_prompt = ChatPromptTemplate.from_template(
    """Analyze the following email and categorize it:
- PROMOTIONAL: Newsletters, marketing campaigns, offers, system advertisements.
- ROUTINE: Regular work requests, inquiries, project questions, recruitment/interview interactions needing replies.
- SENSITIVE: Security alerts, password resets, financial OTPs, confidential data, highly urgent escalations.

Sender: {sender}
Subject: {subject}
Body:
{body}
"""
)
classifier_chain = classify_prompt | classifier_llm

draft_prompt = ChatPromptTemplate.from_template(
    """Draft a professional, courteous, and context-appropriate response to this email:

Sender: {sender}
Subject: {subject}
Original Email Body:
{body}
Summary: {summary}

Draft:"""
)
draft_chain = draft_prompt | llm

current_run_trash: List[Dict[str, str]] = []


# --- 6. LANGGRAPH NODES ---
def classify_node(state: EmailState) -> Dict[str, EmailAnalysis]:
    """LangGraph node: Classifies incoming email into predefined categories.

    Implements retry and exponential backoff logic for handling temporary
    503 (Unavailable) and 429 (Resource Exhausted) errors from the LLM endpoint.

    Args:
        state: Current email graph state.

    Returns:
        Dict[str, EmailAnalysis]: Dictionary updating state with classification results.
    """
    max_retries = 3
    delay = 5

    for attempt in range(max_retries):
        try:
            analysis = classifier_chain.invoke(
                {
                    "sender": state["sender"],
                    "subject": state["subject"],
                    "body": state["body"],
                }
            )
            print(
                f"\n[Scanned] Subject: {state['subject']}\n-> Category: {analysis.category} | Priority: {analysis.priority}"
            )
            return {"analysis": analysis}
        except Exception as e:
            err = str(e)
            if "503" in err or "UNAVAILABLE" in err:
                print(f"⚠️ Server busy (503). Retrying in {delay}s...")
                time.sleep(delay)
                delay *= 2
            elif "429" in err or "RESOURCE_EXHAUSTED" in err:
                print("⚠️ Quota limit hit (429). Pausing for 60 seconds...")
                time.sleep(60)
            else:
                print(f"Classification error: {e}")
                break

    print("⚠️ Max retries reached. Defaulting to ROUTINE.")
    fallback = EmailAnalysis(
        category="ROUTINE", priority="LOW", summary="Could not classify."
    )
    return {"analysis": fallback}


def trash_node(state: EmailState) -> Dict[str, Any]:
    """LangGraph node: Queues promotional emails for bulk deletion review.

    Args:
        state: Current email graph state.

    Returns:
        Dict[str, Any]: Empty state update dictionary.
    """
    global current_run_trash
    current_run_trash.append(
        {
            "email_id": state["email_id"],
            "sender": state["sender"],
            "subject": state["subject"],
        }
    )
    print("Action Taken: Queued for Promotional Trash Review")
    return {}


def draft_node(state: EmailState) -> Dict[str, str]:
    """LangGraph node: Drafts an AI response and creates a Gmail draft.

    Generates a reply using the draft chain, persists it in Gmail as an
    associated thread draft, and alerts Telegram with an interactive approval card.

    Args:
        state: Current email graph state.

    Returns:
        Dict[str, str]: Dictionary updating state with the generated reply text.
    """
    reply_res = draft_chain.invoke(
        {
            "sender": state["sender"],
            "subject": state["subject"],
            "body": state["body"],
            "summary": state["analysis"].summary,
        }
    )

    raw_content = (
        reply_res.content if hasattr(reply_res, "content") else reply_res
    )
    if isinstance(raw_content, list):
        reply = "\n".join(
            part.get("text", str(part)) if isinstance(part, dict) else str(part)
            for part in raw_content
        )
    else:
        reply = str(raw_content)

    raw = create_raw_email(state["sender"], f"Re: {state['subject']}", reply)
    draft = (
        gmail_service.users()
        .drafts()
        .create(
            userId="me",
            body={"message": {"threadId": state["thread_id"], "raw": raw}},
        )
        .execute()
    )

    print("\n" + "=" * 55)
    print("📝 [DRAFT CREATED IN GMAIL]")
    print(f"To:      {state['sender']}")
    print(f"Subject: Re: {state['subject']}")
    print(f"Draft ID: {draft['id']}")
    print("=" * 55)

    send_telegram_draft_review(
        sender=state["sender"],
        subject=f"Re: {state['subject']}",
        draft_body=reply,
        draft_id=draft["id"],
    )
    print("Action Taken: Draft Review Sent to Telegram!")
    return {"generated_reply": reply}


def alert_node(state: EmailState) -> Dict[str, Any]:
    """LangGraph node: Sends an alert for sensitive or high-risk emails.

    Args:
        state: Current email graph state.

    Returns:
        Dict[str, Any]: Empty state update dictionary.
    """
    text = (
        f"🚨 SENSITIVE EMAIL ALERT\n\n"
        f"From: {state['sender']}\n"
        f"Subject: {state['subject']}\n\n"
        f"Summary: {state['analysis'].summary}"
    )
    send_telegram_alert(text)
    print("Action Taken: Telegram Sensitive Alert Sent!")
    return {}


def route_email(state: EmailState) -> str:
    """Conditional edge router: Determines graph branch based on category.

    Args:
        state: Current email graph state containing the classification result.

    Returns:
        str: Next node key name ('trash', 'draft', 'alert', or END).
    """
    category = state["analysis"].category
    if category == "PROMOTIONAL":
        return "trash"
    elif category == "ROUTINE":
        return "draft"
    elif category == "SENSITIVE":
        return "alert"
    return END


# --- 7. WORKFLOW GRAPH BUILD ---
workflow = StateGraph(EmailState)
workflow.add_node("classify", classify_node)
workflow.add_node("trash", trash_node)
workflow.add_node("draft", draft_node)
workflow.add_node("alert", alert_node)

workflow.add_edge(START, "classify")
workflow.add_conditional_edges(
    "classify",
    route_email,
    {"trash": "trash", "draft": "draft", "alert": "alert", END: END},
)
workflow.add_edge("trash", END)
workflow.add_edge("draft", END)
workflow.add_edge("alert", END)

agent = workflow.compile()


# --- 8. RUN AGENT WITH ACCUMULATION ---
def fetch_unprocessed_emails() -> List[Dict[str, str]]:
    """Retrieves unread, unprocessed emails from the primary Gmail inbox.

    Parses MIME components for plain text bodies, falling back to message
    snippets when plain text parts are unavailable.

    Returns:
        List[Dict[str, str]]: A list of dictionaries containing email attributes
            (email_id, thread_id, sender, subject, body).
    """
    processed_ids = load_processed_ids()
    results = (
        gmail_service.users()
        .messages()
        .list(userId="me", q="is:unread", maxResults=50)
        .execute()
    )
    messages = results.get("messages", [])

    email_data_list = []
    for msg in messages:
        msg_id = msg["id"]
        if msg_id in processed_ids:
            continue

        full_msg = (
            gmail_service.users()
            .messages()
            .get(userId="me", id=msg_id, format="full")
            .execute()
        )
        headers = full_msg.get("payload", {}).get("headers", [])

        sender = next(
            (h["value"] for h in headers if h["name"].lower() == "from"),
            "Unknown",
        )
        subject = next(
            (h["value"] for h in headers if h["name"].lower() == "subject"),
            "No Subject",
        )

        body = ""
        payload = full_msg.get("payload", {})
        if "parts" in payload:
            for part in payload["parts"]:
                if part.get("mimeType") == "text/plain":
                    data = part.get("body", {}).get("data", "")
                    if data:
                        body = base64.urlsafe_b64decode(data).decode(
                            errors="ignore"
                        )
                        break
        else:
            data = payload.get("body", {}).get("data", "")
            if data:
                body = base64.urlsafe_b64decode(data).decode(errors="ignore")

        if not body:
            body = full_msg.get("snippet", "")

        email_data_list.append(
            {
                "email_id": msg_id,
                "thread_id": full_msg.get("threadId", msg_id),
                "sender": sender,
                "subject": subject,
                "body": body,
            }
        )

    return email_data_list


def run_agent() -> None:
    """Executes the pipeline loop over unread emails.

    Fetches unprocessed emails, invokes the compiled LangGraph workflow for each,
    tracks processed IDs, merges pending promotional emails with previously queued
    items, and dispatches a consolidated Telegram summary.
    """
    global current_run_trash
    current_run_trash = []

    unprocessed_emails = fetch_unprocessed_emails()
    print(f"Found {len(unprocessed_emails)} new unread emails to process.")

    for email_item in unprocessed_emails:
        state_input: EmailState = {
            "email_id": email_item["email_id"],
            "thread_id": email_item["thread_id"],
            "sender": email_item["sender"],
            "subject": email_item["subject"],
            "body": email_item["body"],
            "analysis": None,
            "generated_reply": None,
        }
        try:
            agent.invoke(state_input)
            save_processed_id(email_item["email_id"])
        except Exception as e:
            print(f"Skipping email due to error: {e}")

        time.sleep(2)

    # Accumulate existing pending trash emails with emails processed in this run
    all_trash = load_pending_trash()
    existing_ids = {item["email_id"] if isinstance(item, dict) else item for item in all_trash}

    for item in current_run_trash:
        if item["email_id"] not in existing_ids:
            all_trash.append(item)
            existing_ids.add(item["email_id"])

    if all_trash:
        save_pending_trash(all_trash)
        print(f"\nSending consolidated summary for {len(all_trash)} promotional emails to Telegram...")
        send_telegram_trash_summary(all_trash)
    else:
        print("No pending promotional emails to trash.")


if __name__ == "__main__":
    run_agent()