# MCP server (Module 4)

`src/timesheet_exception_agent/mcp_server.py` is a read-only [Model Context Protocol](https://modelcontextprotocol.io) server over the lock-day database. Any MCP client, such as Claude Desktop, Claude Code, the MCP Inspector or another agent, can ask about the latest run: which exceptions are open, what managers said, which policy applies, and what's waiting for approval.

It's built on the official `mcp` Python SDK (v2, `MCPServer`), runs over stdio, and makes no model calls.

## Tools

| Tool | Returns |
| --- | --- |
| `list_exceptions(status?)` | Every exception with its status, employee, manager and lock risk (0–1) |
| `get_exception(exception_id)` | Employee, manager with Teams presence, next-level manager, teammates, lock-risk factors, latest fallback estimate |
| `get_messages(exception_id)` | Messages sent and replies received, in order. Replies are marked `untrusted`, with the reply screen's `flagged` reasons |
| `search_policy(query, exception_id?)` | Top 3 policy sections with citations. With an exception, filtered on that employee's state and the run's date |
| `find_similar_cases(exception_id)` | The 3 most similar past resolved cases and the employee's last two closed periods |
| `get_approval_queue()` | Fallback proposals waiting for the coordinator, lowest confidence first, with flags and the runner-up |
| `get_audit_trail(exception_id, limit?)` | The audit log for one exception, oldest first |

All the tools reuse the agents' own code: `OutreachTools`, `RetrievalTools`, `Router.approval_queue` and the reply screen. No business rule lives in the server.

## Design

- **Read-only.** An MCP client is another model. The server has no tool to send a message, change a status or approve time, so the Outreach guardrails (message cap, escalation cut-off, scope, lock) and the coordinator's approval gate, which requires initials, can't be bypassed through it. Every tool is annotated `readOnlyHint`.
- **Audited.** Every call is written to `audit_log` as actor `mcp`, action `mcp_call`, with the tool and its arguments. A refused call (unknown id, empty query, bad limit) is audited as `tool_refused` with its guardrail. Policy and case searches keep their usual audit entries, with the query, filters and sources.
- **Untrusted replies.** Manager replies are returned as data, with the same note the agents get and the deterministic screen's flags.
- **Date filter.** The simulated date for policy filters is the last time the latest run reached, from `audit_log`; with an empty log it's the lock day.
- The embedding model (fastembed) loads on the first search, so the server starts quickly.

The tests are in `tests/test_mcp_server.py`. They use a real MCP client session in memory, in a transaction that is rolled back, with the hash embedder.

## Running it

The server reads `DATABASE_URL` from `.env`. Do a lock-day run first, for example `uv run python -m timesheet_exception_agent.graph`, so there is something to look at.

```powershell
uv run python -m timesheet_exception_agent.mcp_server
```

It waits for a client on stdin and stdout. Normally you don't start it yourself: the client starts it.

### MCP Inspector (browser UI, needs Node.js)

```powershell
npx @modelcontextprotocol/inspector uv --directory . run python -m timesheet_exception_agent.mcp_server
```

### Claude Code

```powershell
claude mcp add timesheet-agent -- uv --directory "<path to this repo>" run python -m timesheet_exception_agent.mcp_server
```

### Claude Desktop

Add this to `%APPDATA%\Claude\claude_desktop_config.json`, then restart Claude Desktop:

```json
{
  "mcpServers": {
    "timesheet-agent": {
      "command": "uv",
      "args": ["--directory", "<path to this repo>", "run", "python", "-m", "timesheet_exception_agent.mcp_server"]
    }
  }
}
```

Example questions:
- What's waiting for my approval, and why is the confidence low?
- Show me what Indira Pell replied on exception 6.
- Which policy covers a manager on leave in Colorado?

## In production

The agent's mock tools (`send_message`, `get_presence`) would be replaced by real email and Teams connectors, which are increasingly available as MCP servers. The agents wouldn't change. This server is the other direction: the agent's own state, offered to the coordinator's assistant.
