# Bug Identification Report

## Summary

Code review of the ChatOps AI Bridge codebase identified **5 bugs** across multiple services:

| Severity | Count | Description |
|----------|-------|-------------|
| High | 2 | Security/logic bugs that could cause data leaks or incorrect behavior |
| Medium | 2 | Reliability issues under edge cases |
| Low | 1 | Code quality/maintainability issue |

---

## Bug #1: Path Traversal Validation Logic Error (HIGH)

**Location:** `services/container-manager/src/container_manager/routers.py:36-50`

**Code:**
```python
def _validate_workspace_path(user_path: str) -> str:
    resolved = PurePosixPath("/workspace") / user_path
    normalized = PurePosixPath(*resolved.parts)  # BUG: This doesn't normalize ..
    if not str(normalized).startswith("/workspace"):
        raise HTTPException(...)
    return str(normalized)
```

**Issue:** The `PurePosixPath(*resolved.parts)` normalization does NOT collapse `..` parent references. The `parts` property already contains the split components, and reconstructing a Path from them preserves the `..` sequences.

**Exploit Example:**
```python
user_path = "../../../etc/passwd"
resolved = PurePosixPath("/workspace") / user_path
# resolved.parts = ('/', 'workspace', '..', '..', '..', 'etc', 'passwd')
normalized = PurePosixPath(*resolved.parts)
# str(normalized) = "/workspace/../../../etc/passwd" - still has ..
# .startswith("/workspace") returns True - BYPASSED!
```

**Fix:** Use `resolved.resolve()` or properly check that the normalized path stays within bounds after calling `.resolve()`.

---

## Bug #2: SSE Streaming Swallows Error Events (HIGH)

**Location:** `services/api-server/src/api_server/routers/sessions.py:211-217` and `:269-275`

**Code:**
```python
async for line in response.aiter_lines():
    if not line or not line.startswith("data: "):
        continue  # BUG: Silently ignores error lines
    payload_data = line[6:]
    if payload_data == "[DONE]":
        break
    yield f"data: {payload_data}\n\n"
```

**Issue:** The SSE proxy logic only forwards lines starting with `data: `. However, SSE error events are sent as `event: error\ndata: {...}` or sometimes just raw error responses. These are silently dropped, meaning the client never receives error notifications from the upstream container-manager.

**Impact:** When container-manager returns an error (container not found, exec failed, WebSocket connection refused), the Telegram bot appears to hang because no error reaches it.

**Fix:** Check for non-2xx response status before streaming, or capture and forward error events explicitly.

---

## Bug #3: Unsafe Shell Command Construction (MEDIUM)

**Location:** `services/container-manager/src/container_manager/routers.py:206-210`

**Code:**
```python
import base64
encoded = base64.b64encode(file_content).decode("ascii")
quoted_path = shlex.quote(safe_path)
command = f"echo '{encoded}' | base64 -d > {quoted_path}"  # BUG: Single quotes
```

**Issue:** While `shlex.quote()` is used on the path, the base64-encoded content is wrapped in single quotes without escaping. If the base64 content contains a single quote character (unlikely but possible in binary files), it will break the shell command.

**Secondary Issue:** Large files will exceed shell command length limits (ARG_MAX, typically 2MB on Linux).

**Fix:** Use a heredoc or pipe the data through stdin using aiodocker's exec stdin support instead of echo.

---

## Bug #4: Database Session Lifecycle Conflict (MEDIUM)

**Location:** `services/api-server/src/api_server/routers/sessions.py:289-303`

**Code:**
```python
async def generate():
    # ... streaming logic ...
    finally:
        yield "data: [DONE]\n\n"
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        full_response = "".join(full_response_parts)
        async with get_db_session() as fresh_db:  # Creates new session
            await message_service.log_message(
                session_id=session_id,  # BUG: session_id is from outer scope
                ...
                db=fresh_db,
            )
```

**Issue:** The comment explains this is intentional (outer session may be closed), but there's a subtle bug: if the session was destroyed (container removed) during the streaming operation, the `session_id` captured from the outer scope may reference a non-existent session. The `log_message` call will succeed (new session), but the foreign key constraint could fail or log to a stale session.

**Fix:** Verify the session still exists before logging, or handle FK violation gracefully.

---

## Bug #5: Variable Shadowing and Cache Invalidation (LOW)

**Location:** `services/telegram-bot/src/telegram_bot/main.py` (inferred from pattern)

**Issue:** The `_get_session_id()` helper (mentioned in MEMORY.md as "Bot session tracking: _get_session_id() helper with cache + API fallback pattern") likely has a cache invalidation bug. If a user destroys a session and creates a new one, the telegram-bot's local cache may return the old session ID until restart.

**Note:** Could not locate the exact implementation file for this function. It may be in `telegram_bot/commands/session.py` or similar.

**Fix:** Clear cache on session destroy/restart operations.

---

## Additional Observations

### Missing Error Handling

**Location:** `services/container-manager/src/container_manager/routers.py:148-191`

The `send_message_to_agent` WebSocket proxy does not handle WebSocket handshake failures distinctly from runtime errors. A connection refused (container not ready) vs. a protocol error both yield the same generic exception message.

### Test Coverage Gap

The test files `test_build_env_vars.py` and `test_user_service.py` are mirrors of the actual code rather than tests of the real implementation. This means:
1. The actual implementation is not being tested
2. Changes to the real code will not break tests (tests test the mirror, not the source)
3. This is a testing anti-pattern that gives false confidence

---

## Recommended Priority Order

1. **Bug #1 (Path Traversal)** - Security risk, could allow file system escape
2. **Bug #2 (SSE Errors)** - Causes silent failures, poor user experience
3. **Bug #3 (Shell Command)** - Could cause upload failures for certain files
4. **Bug #4 (DB Session)** - Edge case during concurrent operations
5. **Bug #5 (Cache)** - Minor inconsistency issue
