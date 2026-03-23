<div align="center">
  <img src="nanobot-feishu_logo.jpg" alt="nanobot" width="500">
</div>

# 🐈 nanobot-feishu: Feishu-Specialized nanobot Fork

A **Feishu-focused** fork of [nanobot](https://github.com/HKUDS/nanobot) — an ultra-lightweight personal AI assistant. This version extends nanobot with enhanced Feishu integration, new tools for PDF parsing, image generation, and session management, while refining the message delivery and context management mechanisms for a more reliable and feature-rich Feishu bot experience.

## 📢 News
- **2026-02-10**: First release of the Feishu-focused nanobot fork!
- **2026-02-24**: Enhance CLI mode with feishu message send support
- **2026-03-01**: Optimize tool call logging in sessions and add support for the table format contents in feishu message
- **2026-03-08**: Switch Feishu delivery to interactive card markdown; simplify `message` tool into rich markdown + file modes

## 🌟 What's Changed

This fork introduces the following modifications on top of the original nanobot project:

### 1. 📄 New Tool: `parse_pdf_mineru`

A document parsing tool powered by the [MinerU](https://mineru.net) v4 batch APIs. It supports both batch URL parsing and batch local-file uploading, then polls batch results and returns extracted Markdown with metadata.

- Supports batch URL mode (`extract/task/batch`) and batch local upload mode (`file-urls/batch` + PUT upload)
- LLM-facing parameters are intentionally minimal to avoid context explosion: `urls`, `paths`, `model_version`, `timeout`, `poll_interval`
- Single-file parsing is handled by batch mode (pass a one-item `urls` or `paths` list)
- Asynchronous polling with configurable timeout and interval
- Supports model version override (`pipeline`/`vlm`/`MinerU-HTML`) and common MinerU options
- Downloads and extracts each `full.md` and `images/` from result ZIP archives
- Configured via `tools.mineru` in `config.json`

### 2. 🖼️ New Tool: `image_generate`

An image generation tool that calls a model API (OpenAI-compatible endpoint) to generate or edit images, with optional direct delivery to Feishu.

- **Text-to-image**: Generate images from a text prompt
- **Image editing**: Accept single or multiple input images for editing tasks
- **Aspect ratio control**: Supports `1:1`, `16:9`, `original`, etc.
- **Feishu integration**: Optionally upload the generated image and send it as a rich post message to Feishu directly
- **Auto-save**: Saves output to `workspace/outputs/images/` by default
- Configured via `tools.image_gen` in `config.json`

### 3. 🗣️ New Tool: `session_manage`

A session management tool that enables the agent to programmatically create, switch, list, inspect, and reset conversation sessions.

- **`create`**: Create a new session with an auto-generated or custom title, optionally activate it immediately
- **`switch`**: Switch the active session to an existing one by key
- **`list`**: List all sessions with titles and timestamps
- **`current`**: Show the currently active session
- **`reset`**: Clear the active session override and fall back to the default channel session

This allows the agent to maintain multiple parallel conversation contexts per user/chat.

### 4. 🔄 Enhanced Feishu Channel

The original Feishu channel implementation has been significantly upgraded:

- **Interactive card messages**: Bot responses are now sent as Feishu `interactive` template cards (instead of `post`), with markdown content injected into template variable `content`
- **Markdown rich content support**: Plain text, images, and mixed text+image content are all sent through markdown content in card messages
- **Markdown local image auto-upload**: For markdown image syntax like `![alt](/abs/path/to/image.png)`, local images are uploaded first and links are replaced with Feishu `image_key`
- **Absolute path requirement for markdown images**: Local markdown image links should use absolute paths to avoid key-resolution errors
- **Image receiving**: The bot can receive image messages from users — images are automatically downloaded via the Feishu API and saved to a configurable media directory
- **Image sending (standalone)**: Existing standalone `image` message sending is retained for compatibility
- **File sending**: Supports uploading and sending files (PDF, DOCX, XLSX, PPTX, etc.) as file messages, with a 30MB size limit
- **Reaction feedback**: Automatically adds a thumbs-up reaction to received messages as a "seen" indicator

### 5. 🔍 Transparent Tool-Call Notifications

The message pushing mechanism has been enhanced to provide visibility into the agent's reasoning process:

- When the agent invokes a tool, a **real-time notification** is pushed to the user showing the tool name and its parameters in a formatted code block
- Tool-call records are also written into the session history, giving the user a clear trace of agent behavior
- This makes the agent's actions fully transparent and easier to debug

### 6. 🛠️ Improved Session Context with Tool-Call History

The session management logic now **records tool-call actions into the conversation context**:

- Each tool invocation is saved as an assistant message (e.g., `🛠️Tool Call: web_search`) in the session history
- This prevents the agent from misjudging similar tasks — by seeing its own prior tool calls, it avoids erroneous tool selection or missed invocations
- Results in more consistent and reliable agent behavior across multi-turn conversations

---

## 🚀 Quick Start

### 1. Install

```bash
git clone https://github.com/Wuuu-uu/nanobot-feishu-specilized.git
cd nanobot-feishu-specilized
pip install -e .
```

For Feishu support, also install the Feishu SDK:

```bash
pip install lark-oapi
```

### 2. Initialize

```bash
nanobot onboard
```

### 3. Configure

By default nanobot stores data in `~/.nanobot`. If you moved it, set `NANOBOT_HOME` first, for example:

```bash
export NANOBOT_HOME=/data/home/scwb307/run/.nanobot
```

Then edit the config file inside that directory, for example `/data/home/scwb307/run/.nanobot/config.json`:

```json
{
  "providers": {
    "openrouter": {
      "apiKey": "sk-or-v1-xxx"
    }
  },
  "agents": {
    "defaults": {
      "model": "anthropic/claude-opus-4-5"
    }
  },
  "channels": {
    "feishu": {
      "enabled": true,
      "appId": "cli_xxx",
      "appSecret": "xxx",
      "encryptKey": "",
      "verificationToken": "",
      "allowFrom": [],
      "cardTemplateId": "AAqK6dMNHUVKE",
      "cardTemplateVersionName": "1.0.0",
      "streamingEnabled": true,
      "streamingPrintFrequencyMsDefault": 70,
      "streamingPrintStepDefault": 1,
      "streamingPrintStrategy": "fast",
      "streamingMaxUpdatesPerSec": 8,
      "streamingFinalizeTimeoutSec": 15
    }
  },
  "tools": {
    "web": {
      "search": {
        "apiKey": "",
        "maxResults": 5
      }
    },
    "exec": {
      "timeout": 60
    },
    "mineru": {
      "api_url": "https://mineru.net/api/v4/extract/task",
      "token": "",
      "model_version": "vlm",
      "timeout": 100
    },
    "image_gen": {
      "api_base": "",
      "api_key": "",
      "model_name": "gemini-3-pro-image-preview",
      "timeout": 120,
      "retry_attempts": 3,
      "retry_backoff_seconds": 1.0,
      "retry_backoff_multiplier": 2.0,
      "retry_max_backoff_seconds": 8.0,
      "retry_status_codes": [408, 409, 425, 429, 500, 502, 503, 504]
    },
    "notion": {
      "enabled": true,
      "api_key": "secret_xxx",
      "database_id": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
      "type_database_map": {
        "notes": "",
        "reports": "",
        "log": "yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy",
        "research": "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
      },
      "type_property": "Type"
    },
    "restrictToWorkspace": false
  }
}
```

### 4. Set Up Feishu Bot

1. Visit [Feishu Open Platform](https://open.feishu.cn/app)
2. Create a new app → Enable **Bot** capability
3. **Permissions**: Add `im:message` (send messages), `im:message:send_as_bot`, `im:resource` (download images), `im:message:readonly` (receive messages), `im:message.p2p_msg:readonly` (receive private messages), `docs:document.content:read` (read cloud document content), `cardkit:card:write` (create/update streaming cards)
> **Note: ** As for multi-user senarios, you also need to add `contact:user.employee_id:readonly` to allow the bot to identify the user's Feishu ID.
4. **Events**: Subscribe to `im.message.receive_v1` (receive messages)
   - Select **Long Connection** (WebSocket) mode — no public IP required
5. Get **App ID** and **App Secret** from "Credentials & Basic Info"
6. Publish the app

### 5. Run

```bash
nanobot gateway
```

Or chat directly via CLI:

```bash
nanobot agent -m "Hello!"
```

---

## Configuration Reference

Config file: `~/.nanobot/config.json` by default, or `$NANOBOT_HOME/config.json` when `NANOBOT_HOME` is set

### 🔌 Providers

| Provider | Purpose | Get API Key |
|----------|---------|-------------|
| `openrouter` | LLM (recommended, access to all models) | [openrouter.ai](https://openrouter.ai) |
| `anthropic` | LLM (Claude direct) | [console.anthropic.com](https://console.anthropic.com) |
| `openai` | LLM (GPT direct) | [platform.openai.com](https://platform.openai.com) |
| `deepseek` | LLM (DeepSeek direct) | [platform.deepseek.com](https://platform.deepseek.com) |
| `groq` | LLM + Voice transcription (Whisper) | [console.groq.com](https://console.groq.com) |
| `gemini` | LLM (Gemini direct) | [aistudio.google.com](https://aistudio.google.com) |

### 🛠️ Tool-Specific API Keys

| Tool | Config Path | Required Keys |
|------|------------|---------------|
| Web Search | `tools.web.search` | `apiKey` ([Serper](https://serper.dev)) |
| MinerU PDF | `tools.mineru` | `token` ([MinerU](https://mineru.net)) |
| Image Generation | `tools.image_gen` | `apiBase`, `apiKey`, `modelName` |
| Notion Dataset | `tools.notion` | `apiKey`, `databaseId` |

### 📡 Feishu Channel

| Field | Description |
|-------|-------------|
| `appId` | App ID from Feishu Open Platform |
| `appSecret` | App Secret from Feishu Open Platform |
| `encryptKey` | Encrypt Key (optional for WebSocket mode) |
| `verificationToken` | Verification Token (optional for WebSocket mode) |
| `allowFrom` | Allowed user `open_id` list; empty = allow all |
| `mediaDir` | Directory to save received media (default: `~/.nanobot/media`, or `$NANOBOT_HOME/media` when set) |
| `cardTemplateId` | Feishu card template ID for interactive messages (default: `AAqK6dMNHUVKE`) |
| `cardTemplateVersionName` | Feishu card template version (default: `1.0.0`) |

### 📨 Message Tool Usage (Updated)

`message` tool is simplified into two message categories:

1. **Rich markdown content**
- Use `content` to send plain text, image-only, or mixed text+image messages.
- Images in markdown should use absolute local paths:
  - `![xxxxxx](/data/.../images/fig1_nat.png)`

2. **File messages**
- Keep using existing file parameters (`file_path`, `file_base64`) or non-image entries in `media`.

Notes:
- Local markdown images are auto-uploaded and replaced with Feishu `image_key` at send time.
- Existing standalone image sending logic is preserved for compatibility.

<details>
<summary><b>Full config example</b></summary>

```json
{
  "agents": {
    "defaults": {
      "workspace": "$NANOBOT_HOME/workspace",
      "model": "openai/Claude-Sonnet-4.5",
      "maxTokens": 10240,
      "temperature": 0.7,
      "maxToolIterations": 50
    }
  },
  "providers": {
    "openrouter": {
      "apiKey": "sk-or-v1-xxx"
    }
  },
  "channels": {
    "feishu": {
      "enabled": true,
      "appId": "cli_xxx",
      "appSecret": "xxx",
      "encryptKey": "",
      "verificationToken": "",
      "allowFrom": [],
      "cardTemplateId": "AAqK6dMNHUVKE",
      "cardTemplateVersionName": "1.0.0"
    }
  },
  "tools": {
    "web": {
      "search": {
        "apiKey": "",
        "maxResults": 5
      }
    },
    "exec": {
      "timeout": 60
    },
    "mineru": {
      "api_url": "https://mineru.net/api/v4/extract/task",
      "token": "",
      "model_version": "vlm",
      "timeout": 100
    },
    "image_gen": {
      "api_base": "",
      "api_key": "",
      "model_name": "gemini-3-pro-image-preview",
      "timeout": 120,
      "retry_attempts": 3,
      "retry_backoff_seconds": 1.0,
      "retry_backoff_multiplier": 2.0,
      "retry_max_backoff_seconds": 8.0,
      "retry_status_codes": [408, 409, 425, 429, 500, 502, 503, 504]
    },
    "notion": {
      "enabled": true,
      "api_key": "",
      "database_id": "",
      "type_database_map": {
        "notes": "",
        "reports": "",
        "log": "",
        "research": "",
        "archive": ""
      },
      "type_property": "Type"
    },
    "restrictToWorkspace": false
  }
}
```

</details>

---

## CLI Reference

| Command | Description |
|---------|-------------|
| `nanobot onboard` | Initialize config & workspace |
| `nanobot agent -m "..."` | Chat with the agent |
| `nanobot agent` | Interactive chat mode |
| `nanobot gateway` | Start the gateway (Feishu bot) |
| `nanobot status` | Show status |
| `nanobot cron add` | Add a scheduled task |
| `nanobot cron list` | List scheduled tasks |
| `nanobot cron remove <id>` | Remove a scheduled task |

---

## 📁 Project Structure

```
nanobot/
├── agent/                # Core agent logic
│   ├── loop.py           #   Agent loop (LLM ↔ tool execution)
│   ├── context.py        #   Prompt & context builder
│   ├── memory.py         #   Persistent memory
│   ├── skills.py         #   Skills loader
│   ├── subagent.py       #   Background task execution
│   └── tools/            #   Built-in tools
│       ├── base.py       #     Tool base class
│       ├── registry.py   #     Dynamic tool registry
│       ├── filesystem.py #     File read/write/edit/list
│       ├── shell.py      #     Shell command execution
│       ├── web.py        #     Web search & fetch
│       ├── message.py    #     Message sending (rich markdown + file)
│       ├── pdf_mineru.py #     ★ PDF parsing via MinerU API
│       ├── image_generate.py # ★ Image generation & Feishu delivery
│       ├── session_manage.py # ★ Session create/switch/reset
│       ├── spawn.py      #     Subagent spawning
│       └── cron.py       #     Cron task management
├── channels/             # Chat channel integrations
│   ├── base.py           #   Base channel interface
│   ├── feishu.py         #   ★ Enhanced Feishu (interactive markdown card, image, file)
│   ├── telegram.py       #   Telegram
│   ├── discord.py        #   Discord
│   └── whatsapp.py       #   WhatsApp
├── session/              # Conversation session management
│   └── manager.py        #   ★ Session CRUD with active session tracking
├── bus/                  # Message routing (event bus)
├── cron/                 # Scheduled tasks
├── heartbeat/            # Proactive wake-up
├── providers/            # LLM providers (LiteLLM-based)
├── config/               # Configuration schema & loader
├── skills/               # Bundled skills (github, weather, tmux...)
├── cli/                  # CLI commands
└── utils/                # Helpers
```

> Items marked with ★ are new or significantly modified in this fork.

---

## 🙏 Acknowledgements

This project is based on [nanobot](https://github.com/HKUDS/nanobot) by [HKUDS](https://github.com/HKUDS). Licensed under [MIT](./LICENSE).
