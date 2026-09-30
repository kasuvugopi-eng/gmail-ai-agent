# 📬 Autonomous Gmail AI Agent with Telegram Bot Integration

An intelligent, autonomous email workflow automation system built with **LangGraph**, **Google Gemini**, and the **Gmail API**. The agent actively monitors unread emails, intelligently classifies them into actionable categories, generates contextual draft responses, sends instant Telegram notifications with interactive action buttons, and supports document attachments directly from Telegram.

---

## 🌟 Key Features

- **Automated Email Classification:** Powered by Google Gemini to analyze and categorize incoming emails into:
  - `ROUTINE`: Inquiries, project updates, regular work communication requiring responses.
  - `PROMOTIONAL`: Newsletters, marketing campaigns, offers.
  - `SENSITIVE`: Critical alerts, OTPs, urgent escalations (instant Telegram notifications sent).
- **Automated Contextual Drafts:** Automatically generates professional replies and creates drafts in Gmail without sending them blindly.
- **Telegram Interactive Control:** 
  - Inline buttons (`[ ✅ Send Reply ]`, `[ ❌ Keep Draft ]`) to approve or hold drafts directly from Telegram.
  - One-click batch deletion for accumulated promotional emails (`[ 🗑️ Delete All Promotional Mails ]`).
- **Targeted Document Attachments:** Reply directly to any draft notification in Telegram with a file (PDF, DOCX, PNG, etc.) to attach it to that specific Gmail draft before sending.
- **State Persistence & Resumption:** Uses `processed_ids.json` to prevent re-processing existing emails across restarts and `pending_trash.json` to accumulate promotional emails.
- **Built-in Resilience:** Exponential backoff and retry handling for Google API 503 (temporary high demand) and 429 (rate limits).

---

## 🛠️ Tech Stack

- **Runtime & Environment:** Python 3.11+, [uv](https://github.com/astral-sh/uv)
- **Agent Orchestration:** LangGraph, LangChain
- **LLM:** `gemini-3.8-flash` via `langchain-google-genai`
- **APIs:** Google Gmail REST API, Telegram Bot API
- **Data Validation:** Pydantic

---

## 📁 Project Structure

```text
gmail_agent/
│
├── main.py               # Core LangGraph pipeline: fetches, classifies, drafts, and notifies
├── bot_listener.py       # Telegram polling listener for buttons and file attachments
├── pyproject.toml        # Dependencies and environment metadata
├── uv.lock               # Deterministic dependency lockfile
├── .env.example          # Environment variables template
├── .gitignore            # Git exclusion rules for secrets and local state
└── README.md             # Project documentation


🚀 Setup & Installation
1. Prerequisites
Python 3.11 or later installed.

uv package manager installed.

A Google Cloud Project with the Gmail API enabled and OAuth 2.0 Client Credentials downloaded as credentials.json.

A Telegram Bot token obtained from @BotFather and your personal Telegram chat_id.

2. Clone the Repository
Bash
git clone [https://github.com/](https://github.com/)<YOUR_USERNAME>/<YOUR_REPO_NAME>.git
cd <YOUR_REPO_NAME>
3. Install Dependencies with uv
Bash
uv sync
4. Configure Environment Variables
Copy .env.example to .env:

Bash
cp .env.example .env
Fill in your configuration:

Code snippet
GOOGLE_API_KEY=your_gemini_api_key_here
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_numeric_chat_id
5. Google OAuth Setup
Place your downloaded credentials.json in the root project directory. On the very first run, a browser tab will open asking for Gmail authorization. After approval, token.json will be generated automatically.

🚦 Running the Agent
Open two terminal windows to run both the pipeline agent and the Telegram listener concurrently:

Terminal 1: Start Telegram Listener
Keep this running in the background to handle button clicks and incoming attachment uploads:

PowerShell
uv run --active python bot_listener.py
Terminal 2: Run Email Processor
Execute the scanner to process unread emails incrementally:

PowerShell
uv run --active python main.py
💡 Telegram Usage Guide
Approving Drafts: Click [ ✅ Send Reply ] under the draft summary to dispatch the email immediately via Gmail API.

Keeping Drafts: Click [ ❌ Keep Draft ] to retain the draft in your Gmail web client for manual edits.

Attaching Files: Swipe left or click Reply on any draft notification in Telegram and send a document/PDF. The bot will automatically attach the file to that specific draft.

Batch Trash: When promotional emails accumulate, click [ 🗑️ Delete All Promotional Mails ] to batch-move them into Gmail Trash.

🔒 Security Notice
Never commit credentials.json, token.json, or .env to version control. Ensure all sensitive tokens and local state files remain listed in .gitignore.