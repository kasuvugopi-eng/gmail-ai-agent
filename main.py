import base64
from email.message import EmailMessage
import json
import os
import time
from typing import Literal, Optional, TypedDict

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
import requests

load_dotenv()

# --- 1. CONFIGURATION & LOG TRACKING ---
SCOPES = ["https://mail.google.com/"]
LOG_FILE = "processed_ids.json"
PENDING_TRASH_FILE = "pending_trash.json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip().strip('"').strip("'")

if TELEGRAM_BOT_TOKEN.startswith("bot"):
    TELEGRAM_BOT_TOKEN = TELEGRAM_BOT_TOKEN[3:]


def load_processed_ids() -> set:
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_processed_id(email_id: str):
    ids = load_processed_ids()
    ids.add(email_id)
    with open(LOG_FILE, "w") as f:
        json.dump(list(ids), f)


def load_pending_trash() -> list:
    if os.path.exists(PENDING_TRASH_FILE):
        try:
            with open(PENDING_TRASH_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_pending_trash(trash_list: list):
    with open(PENDING_TRASH_FILE, "w") as f:
        json.dump(trash_list, f, indent=2)


# --- 2. DATA SCHEMAS ---
class EmailAnalysis(BaseModel):
    category: Literal["PROMOTIONAL", "ROUTINE", "SENSITIVE"] = Field(
        description="The classification category of the email."
    )
    priority: Literal["LOW", "MEDIUM", "HIGH"] = Field(
        description="The priority of the email."
    )
    summary: str = Field(
        description="A crisp 1-2 sentence summary of what the email is about."
    )


class EmailState(TypedDict):
    email_id: str
    thread_id: str
    sender: str
    subject: str
    body: str
    analysis: Optional[EmailAnalysis]
    generated_reply: Optional[str]


# --- 3. TELEGRAM UTILITY FUNCTIONS ---
def send_telegram_alert(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram alert error: {e}")


def send_telegram_draft_review(
    sender: str, subject: str, draft_body: str, draft_id: str
):
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


def send_telegram_trash_summary(all_trash_list: list):
    if not all_trash_list:
        return

    count = len(all_trash_list)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    text = f"🗑️ *PENDING PROMOTIONAL EMAILS ({count} total)*\n\n"
    
    # మొదటి 15 ప్రివ్యూగా చూపించడం
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

# --- 4. GMAIL AUTHENTICATION & HELPERS ---
def get_gmail_service():
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
        with open("token.json", "w") as token:
            token.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


gmail_service = get_gmail_service()


def create_raw_email(to: str, subject: str, message_text) -> str:
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
llm = ChatGoogleGenerativeAI(model="gemini-3.8-flash")
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

current_run_trash = []


# --- 6. LANGGRAPH NODES ---
def classify_node(state: EmailState) -> dict:
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


def trash_node(state: EmailState) -> dict:
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


def draft_node(state: EmailState) -> dict:
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


def alert_node(state: EmailState) -> dict:
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
def fetch_unprocessed_emails():
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


def run_agent():
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

    # 36 పాత మెయిల్స్ + కొత్త మెయిల్స్ అక్యుమ్యులేట్ చేయడం
    
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