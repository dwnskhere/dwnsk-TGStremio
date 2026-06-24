#!/bin/bash
# ============================================================================
# Telegram-Stremio Upstream Sync Script (with Telegram Integration)
# ============================================================================
# All-in-one script: Telegram helper functions + sync logic + test mode
# Designed to run inside GitHub Actions. All config via environment variables.
#
# Modes (controlled by env vars):
#   TEST_MODE=true   → Test Telegram bot connection only
#   DRY_RUN=true     → Preview commits without cherry-picking
#   (default)        → Full sync with Telegram notifications
# ============================================================================

set -euo pipefail

# ╔════════════════════════════════════════════════════════════════════════════╗
# ║                           CONFIGURATION                                  ║
# ╚════════════════════════════════════════════════════════════════════════════╝

UPSTREAM_REMOTE="${UPSTREAM_REMOTE:-upstream}"
UPSTREAM_URL="${UPSTREAM_URL:-https://github.com/weebzone/Telegram-Stremio.git}"
UPSTREAM_BRANCH="${UPSTREAM_BRANCH:-master}"
LOCAL_BRANCH="${LOCAL_BRANCH:-master}"
MAX_SEARCH_DEPTH="${MAX_SEARCH_DEPTH:-500}"
MAX_LOCAL_SEARCH_DEPTH="${MAX_LOCAL_SEARCH_DEPTH:-50}"
DRY_RUN="${DRY_RUN:-false}"
TEST_MODE="${TEST_MODE:-false}"

# ── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

info()    { echo -e "${BLUE}ℹ${NC}  $*"; }
success() { echo -e "${GREEN}✔${NC}  $*"; }
warn()    { echo -e "${YELLOW}⚠${NC}  $*"; }
error()   { echo -e "${RED}✖${NC}  $*"; }
step()    { echo -e "\n${BOLD}${CYAN}══ $* ══${NC}"; }
divider() { echo -e "${DIM}$(printf '%.0s─' {1..60})${NC}"; }

# ╔════════════════════════════════════════════════════════════════════════════╗
# ║                       TELEGRAM HELPER FUNCTIONS                          ║
# ╚════════════════════════════════════════════════════════════════════════════╝

TG_API_BASE="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN:-}"
TG_TIMEOUT="${TELEGRAM_TIMEOUT:-300}"
TG_SESSION_ID="$(date +%s)$$"
TG_ENABLED="true"

# Parse chat_id and topic_id (format: chat_id|topic_id or simple chat_id)
TG_CHAT_ID="${TELEGRAM_CHAT_ID%%|*}"
if [[ "${TELEGRAM_CHAT_ID:-}" == *"|"* ]]; then
    TG_THREAD_ID="${TELEGRAM_CHAT_ID#*|}"
else
    TG_THREAD_ID=""
fi

# Check if Telegram is configured
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    TG_ENABLED="false"
    warn "Telegram credentials not set. Running without notifications."
fi

# ── tg_validate: Validate bot token ────────────────────────────────────────
tg_validate() {
    if [ "$TG_ENABLED" != "true" ]; then
        error "Telegram not configured."
        return 1
    fi

    if ! command -v jq &>/dev/null; then
        error "'jq' is required but not installed."
        return 1
    fi

    local me
    me=$(curl -s --max-time 10 "${TG_API_BASE}/getMe")
    if [ "$(echo "$me" | jq -r '.ok')" != "true" ]; then
        error "Telegram API check failed. Is your BOT_TOKEN valid?"
        echo "  Response: $me" >&2
        return 1
    fi

    local bot_name
    bot_name=$(echo "$me" | jq -r '.result.username')
    success "Telegram bot connected: @${bot_name}"
    return 0
}

# ── tg_escape: Escape HTML entities ────────────────────────────────────────
tg_escape() {
    local text="$1"
    text="${text//&/&amp;}"
    text="${text//</&lt;}"
    text="${text//>/&gt;}"
    echo "$text"
}

# ── tg_send_message: Send a text message ───────────────────────────────────
# Usage: msg_id=$(tg_send_message "Hello <b>bold</b>")
tg_send_message() {
    [ "$TG_ENABLED" != "true" ] && return 0
    local text="$1"

    local payload
    payload=$(jq -n \
        --arg chat_id "$TG_CHAT_ID" \
        --arg thread_id "$TG_THREAD_ID" \
        --arg text "$text" \
        '{
            chat_id: $chat_id,
            text: $text,
            parse_mode: "HTML",
            disable_web_page_preview: true
        } + (if $thread_id != "" then {message_thread_id: ($thread_id | tonumber)} else {} end)')

    local response
    response=$(curl -s --max-time 15 -X POST "${TG_API_BASE}/sendMessage" \
        -H "Content-Type: application/json" \
        -d "$payload")

    if [ "$(echo "$response" | jq -r '.ok')" != "true" ]; then
        warn "Failed to send Telegram message" >&2
        echo "" 
        return 1
    fi

    echo "$response" | jq -r '.result.message_id'
}

# ── tg_send_inline_keyboard: Send message with buttons ────────────────────
# Usage: msg_id=$(tg_send_inline_keyboard "text" "Btn1" "data1" "Btn2" "data2")
tg_send_inline_keyboard() {
    [ "$TG_ENABLED" != "true" ] && return 0
    local text="$1"
    local btn1_text="$2"
    local btn1_data="${TG_SESSION_ID}_${3}"
    local btn2_text="$4"
    local btn2_data="${TG_SESSION_ID}_${5}"

    local payload
    payload=$(jq -n \
        --arg chat_id "$TG_CHAT_ID" \
        --arg thread_id "$TG_THREAD_ID" \
        --arg text "$text" \
        --arg b1t "$btn1_text" \
        --arg b1d "$btn1_data" \
        --arg b2t "$btn2_text" \
        --arg b2d "$btn2_data" \
        '{
            chat_id: $chat_id,
            text: $text,
            parse_mode: "HTML",
            disable_web_page_preview: true,
            reply_markup: {
                inline_keyboard: [[
                    { text: $b1t, callback_data: $b1d },
                    { text: $b2t, callback_data: $b2d }
                ]]
            }
        } + (if $thread_id != "" then {message_thread_id: ($thread_id | tonumber)} else {} end)')

    local response
    response=$(curl -s --max-time 15 -X POST "${TG_API_BASE}/sendMessage" \
        -H "Content-Type: application/json" \
        -d "$payload")

    if [ "$(echo "$response" | jq -r '.ok')" != "true" ]; then
        warn "Failed to send inline keyboard" >&2
        echo ""
        return 1
    fi

    echo "$response" | jq -r '.result.message_id'
}

# ── tg_wait_for_callback: Poll for button press ───────────────────────────
# Returns: callback data suffix (e.g. "push_yes") or "TIMEOUT"
tg_wait_for_callback() {
    [ "$TG_ENABLED" != "true" ] && echo "TIMEOUT" && return 1
    local timeout="${TG_TIMEOUT}"
    local start_time
    start_time=$(date +%s)
    local expected_prefix="${TG_SESSION_ID}_"

    # Flush old updates
    local flush_response
    flush_response=$(curl -s --max-time 10 "${TG_API_BASE}/getUpdates?offset=-1&timeout=0")
    local last_update_id
    last_update_id=$(echo "$flush_response" | jq -r '.result[-1].update_id // empty')

    local offset=""
    if [ -n "$last_update_id" ]; then
        offset=$((last_update_id + 1))
    fi

    info "Waiting for Telegram response (timeout: ${timeout}s)..." >&2

    while true; do
        local now
        now=$(date +%s)
        local elapsed=$((now - start_time))

        if [ "$elapsed" -ge "$timeout" ]; then
            echo "TIMEOUT"
            return 1
        fi

        local remaining=$((timeout - elapsed))
        local poll_timeout=$((remaining < 30 ? remaining : 30))

        local url="${TG_API_BASE}/getUpdates?timeout=${poll_timeout}&allowed_updates=%5B%22callback_query%22%5D"
        if [ -n "$offset" ]; then
            url="${url}&offset=${offset}"
        fi

        local updates
        updates=$(curl -s --max-time $((poll_timeout + 5)) "$url")

        if [ "$(echo "$updates" | jq -r '.ok')" != "true" ]; then
            warn "getUpdates failed, retrying..." >&2
            sleep 2
            continue
        fi

        local count
        count=$(echo "$updates" | jq '.result | length')

        if [ "$count" -gt 0 ]; then
            local i
            for i in $(seq 0 $((count - 1))); do
                local update_id callback_data callback_id
                update_id=$(echo "$updates" | jq -r ".result[$i].update_id")
                callback_data=$(echo "$updates" | jq -r ".result[$i].callback_query.data // empty")
                callback_id=$(echo "$updates" | jq -r ".result[$i].callback_query.id // empty")

                offset=$((update_id + 1))

                if [ -n "$callback_data" ] && [[ "$callback_data" == ${expected_prefix}* ]]; then
                    tg_answer_callback "$callback_id" "✅ Received!"
                    echo "${callback_data#${expected_prefix}}"
                    return 0
                fi

                # Answer unrelated callbacks silently
                if [ -n "$callback_id" ]; then
                    tg_answer_callback "$callback_id" ""
                fi
            done
        fi
    done
}

# ── tg_answer_callback: Acknowledge button press ──────────────────────────
tg_answer_callback() {
    local callback_id="$1"
    local text="${2:-}"

    local payload
    payload=$(jq -n \
        --arg id "$callback_id" \
        --arg text "$text" \
        '{ callback_query_id: $id, text: $text }')

    curl -s --max-time 10 -X POST "${TG_API_BASE}/answerCallbackQuery" \
        -H "Content-Type: application/json" \
        -d "$payload" > /dev/null 2>&1
}

# ── tg_edit_message: Edit a sent message ───────────────────────────────────
tg_edit_message() {
    [ "$TG_ENABLED" != "true" ] && return 0
    local message_id="$1"
    local new_text="$2"

    local payload
    payload=$(jq -n \
        --arg chat_id "$TG_CHAT_ID" \
        --argjson msg_id "$message_id" \
        --arg text "$new_text" \
        '{
            chat_id: $chat_id,
            message_id: $msg_id,
            text: $text,
            parse_mode: "HTML",
            disable_web_page_preview: true
        }')

    curl -s --max-time 10 -X POST "${TG_API_BASE}/editMessageText" \
        -H "Content-Type: application/json" \
        -d "$payload" > /dev/null 2>&1
}

# ╔════════════════════════════════════════════════════════════════════════════╗
# ║                            TEST MODE                                     ║
# ╚════════════════════════════════════════════════════════════════════════════╝

if [ "$TEST_MODE" = "true" ]; then
    step "Telegram Bot Test"

    echo "::group::Test 1: Validate Bot"
    if tg_validate; then
        success "Bot is valid and connected!"
    else
        error "Bot validation failed."
        exit 1
    fi
    echo "::endgroup::"

    echo "::group::Test 2: Send Message"
    MSG_ID=$(tg_send_message "🧪 <b>Telegram-Stremio Sync Bot Test</b>

This is a test message from GitHub Actions.
If you see this, your bot is working correctly!

⏰ Sent at: $(date '+%Y-%m-%d %I:%M:%S %p %Z')")

    if [ -n "$MSG_ID" ]; then
        success "Message sent! (ID: ${MSG_ID})"
    else
        error "Failed to send message."
        exit 1
    fi
    echo "::endgroup::"

    echo "::group::Test 3: Inline Buttons + Callback"
    BTN_MSG_ID=$(tg_send_inline_keyboard \
        "🔘 <b>Button Test</b>

Click a button to verify callbacks work:" \
        "✅ Yes!" "test_yes" \
        "❌ No!" "test_no")

    if [ -n "$BTN_MSG_ID" ]; then
        success "Inline keyboard sent! (ID: ${BTN_MSG_ID})"
    else
        error "Failed to send inline keyboard."
        exit 1
    fi

    # Short timeout for test
    TG_TIMEOUT=60
    info "Waiting up to 60s for button press..."

    CALLBACK_RESULT=$(tg_wait_for_callback) || true

    case "$CALLBACK_RESULT" in
        test_yes)
            success "Received: YES clicked!"
            tg_edit_message "$BTN_MSG_ID" "🔘 <b>Button Test</b>

You clicked: ✅ <b>Yes!</b>
✅ All tests passed!"
            ;;
        test_no)
            success "Received: NO clicked!"
            tg_edit_message "$BTN_MSG_ID" "🔘 <b>Button Test</b>

You clicked: ❌ <b>No!</b>
✅ All tests passed!"
            ;;
        TIMEOUT)
            warn "Timed out (this is OK — timeout detection works)."
            tg_edit_message "$BTN_MSG_ID" "🔘 <b>Button Test</b>

⏰ Timed out. No button was clicked.
✅ Bot is working, timeout detection confirmed."
            ;;
    esac
    echo "::endgroup::"

    step "All Tests Passed ✅"
    exit 0
fi

# ╔════════════════════════════════════════════════════════════════════════════╗
# ║                          SYNC LOGIC                                      ║
# ╚════════════════════════════════════════════════════════════════════════════╝

# ── Validate Telegram ──────────────────────────────────────────────────────
if [ "$TG_ENABLED" = "true" ]; then
    step "Telegram Setup"
    if ! tg_validate; then
        error "Telegram validation failed."
        exit 1
    fi
fi

# ── Pre-flight Checks ──────────────────────────────────────────────────────
step "Pre-flight Checks"

if ! git rev-parse --is-inside-work-tree &>/dev/null; then
    error "Not inside a git repository."
    tg_send_message "❌ <b>Sync Failed</b>

Not inside a git repository." > /dev/null 2>&1
    exit 1
fi
success "Inside git repository"

if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
    error "Working tree is not clean."
    tg_send_message "❌ <b>Sync Failed</b>

Working tree is not clean." > /dev/null 2>&1
    exit 1
fi
success "Working tree is clean"

# Switch to the correct branch if needed
CURRENT_BRANCH=$(git branch --show-current)
if [ "$CURRENT_BRANCH" != "$LOCAL_BRANCH" ]; then
    info "Switching from '${CURRENT_BRANCH}' to '${LOCAL_BRANCH}'..."
    git checkout "$LOCAL_BRANCH"
fi
success "On branch ${BOLD}${LOCAL_BRANCH}${NC}"

# ── Step 1: Read Latest Local Commit ───────────────────────────────────────
step "Step 1: Reading Latest Local Commit"

LOCAL_COMMIT_HASH=$(git rev-parse HEAD)
LOCAL_COMMIT_MSG=$(git log -1 --format='%s' HEAD)
LOCAL_COMMIT_DATE=$(git log -1 --format='%ci' HEAD)

echo -e "  ${DIM}Hash:${NC}    ${LOCAL_COMMIT_HASH:0:10}"
echo -e "  ${DIM}Message:${NC} ${LOCAL_COMMIT_MSG}"
echo -e "  ${DIM}Date:${NC}    ${LOCAL_COMMIT_DATE}"

ESCAPED_MSG=""
[ "$TG_ENABLED" = "true" ] && ESCAPED_MSG=$(tg_escape "$LOCAL_COMMIT_MSG")

tg_send_message "🔄 <b>Telegram-Stremio Sync Started</b>

📋 <b>Latest local commit:</b>
<code>${LOCAL_COMMIT_HASH:0:10}</code> ${ESCAPED_MSG}

⬇️ Fetching upstream <code>${UPSTREAM_BRANCH}</code>..." > /dev/null 2>&1

# ── Step 2: Fetch Upstream ─────────────────────────────────────────────────
step "Step 2: Fetching Upstream"

if git remote get-url "$UPSTREAM_REMOTE" &>/dev/null; then
    EXISTING_URL=$(git remote get-url "$UPSTREAM_REMOTE")
    if [ "$EXISTING_URL" != "$UPSTREAM_URL" ]; then
        warn "Updating upstream remote URL..."
        git remote set-url "$UPSTREAM_REMOTE" "$UPSTREAM_URL"
    fi
    success "Upstream remote exists"
else
    info "Adding upstream remote..."
    git remote add "$UPSTREAM_REMOTE" "$UPSTREAM_URL"
    success "Added upstream remote"
fi

info "Fetching upstream/${UPSTREAM_BRANCH}..."
git fetch "$UPSTREAM_REMOTE" "$UPSTREAM_BRANCH"
success "Fetched upstream/${UPSTREAM_BRANCH}"

# ── Step 3: Find Matching Commit ──────────────────────────────────────────
step "Step 3: Finding Matching Commit in Upstream"

info "Scanning recent local commits to match with upstream history..."

MATCH_HASH=""
MATCH_LOCAL_HASH=""
MATCH_MSG=""
SEARCH_COUNT=0

# Get local history
mapfile -t LOCAL_COMMITS < <(git log --no-merges --format='%H|%s' -n "$MAX_LOCAL_SEARCH_DEPTH")
# Get upstream history
mapfile -t UPSTREAM_COMMITS < <(git log --no-merges --format='%H|%s' -n "$MAX_SEARCH_DEPTH" "${UPSTREAM_REMOTE}/${UPSTREAM_BRANCH}")

REVERTED_LIST=()

for local_line in "${LOCAL_COMMITS[@]}"; do
    LOCAL_HASH="${local_line%%|*}"
    LOCAL_MSG="${local_line#*|}"
    
    [ -z "$LOCAL_MSG" ] && continue
    
    # Normalize local commit message by stripping trailing PR numbers (e.g. " (#580)")
    NORM_LOCAL_MSG=$(echo "$LOCAL_MSG" | sed -E 's/ \(_*#[0-9]+\)$//')
    NORM_LOCAL_MSG=$(echo "$NORM_LOCAL_MSG" | sed -E 's/ \(#[0-9]+\)$//')
    
    # Check if this local commit is a revert commit
    if [[ "$LOCAL_MSG" =~ ^Revert\ \"(.*)\"$ ]]; then
        REVERTED_MSG="${BASH_REMATCH[1]}"
        REVERTED_LIST+=("$REVERTED_MSG")
        info "Detected local revert for: \"${REVERTED_MSG}\". Will allow re-cherry-picking."
        continue
    fi
    
    # If this commit message was reverted, skip matching it
    IS_REVERTED="false"
    for rev in "${REVERTED_LIST[@]}"; do
        NORM_REV=$(echo "$rev" | sed -E 's/ \(#[0-9]+\)$//')
        if [ "$NORM_REV" = "$NORM_LOCAL_MSG" ]; then
            IS_REVERTED="true"
            break
        fi
    done
    
    if [ "$IS_REVERTED" = "true" ]; then
        warn "Skipping match for reverted commit: \"${LOCAL_MSG}\""
        continue
    fi
    
    for upstream_line in "${UPSTREAM_COMMITS[@]}"; do
        SEARCH_COUNT=$((SEARCH_COUNT + 1))
        UPSTREAM_HASH="${upstream_line%%|*}"
        UPSTREAM_MSG="${upstream_line#*|}"
        NORM_UPSTREAM_MSG=$(echo "$UPSTREAM_MSG" | sed -E 's/ \(#[0-9]+\)$//')
        
        if [ "$NORM_UPSTREAM_MSG" = "$NORM_LOCAL_MSG" ]; then
            MATCH_HASH="$UPSTREAM_HASH"
            MATCH_LOCAL_HASH="$LOCAL_HASH"
            MATCH_MSG="$LOCAL_MSG"
            break 2
        fi
    done
done

if [ -z "$MATCH_HASH" ]; then
    error "Could not find any matching commit in upstream history (searched ${#LOCAL_COMMITS[@]} local and ${#UPSTREAM_COMMITS[@]} upstream commits)."
    echo ""
    info "Your latest local commits checked:"
    git log --format="  %C(yellow)%h%C(reset) %s" -n 5
    echo ""
    info "Last 5 upstream commits:"
    git log --format="  %C(yellow)%h%C(reset) %s" -n 5 "${UPSTREAM_REMOTE}/${UPSTREAM_BRANCH}"

    tg_send_message "❌ <b>Sync Failed</b>

Could not find any matching commit in upstream history.

<b>Your latest local commit:</b>
<code>${ESCAPED_MSG}</code>

Searched ${#LOCAL_COMMITS[@]} local and ${#UPSTREAM_COMMITS[@]} upstream commits. The repos may have completely diverged." > /dev/null 2>&1

    exit 1
fi

success "Found match at upstream commit ${MATCH_HASH:0:10} (matching local commit ${MATCH_LOCAL_HASH:0:10}: \"${MATCH_MSG}\")"

# ── Step 4: Identify New Commits ──────────────────────────────────────────
step "Step 4: Identifying New Commits"

mapfile -t RAW_COMMITS < <(git log --no-merges --format='%H' --reverse "${MATCH_HASH}..${UPSTREAM_REMOTE}/${UPSTREAM_BRANCH}")

# Filter out commits whose patches are already applied to local branch (detected via git cherry)
mapfile -t APPLIED_HASHES < <(git cherry "$LOCAL_BRANCH" "${UPSTREAM_REMOTE}/${UPSTREAM_BRANCH}" 2>/dev/null | grep '^-' | awk '{print $2}')

COMMITS=()
for commit in "${RAW_COMMITS[@]}"; do
    IS_APPLIED="false"
    for app_hash in "${APPLIED_HASHES[@]}"; do
        if [ "$commit" = "$app_hash" ]; then
            IS_APPLIED="true"
            break
        fi
    done
    if [ "$IS_APPLIED" = "false" ]; then
        COMMITS+=("$commit")
    fi
done

TOTAL=${#COMMITS[@]}

if [ "$TOTAL" -eq 0 ]; then
    success "Already up to date! No new commits. 🎉"

    tg_send_message "✅ <b>Already Up to Date</b>

No new commits found in upstream. Your branch is synced! 🎉" > /dev/null 2>&1

    exit 0
fi

info "Found ${BOLD}${TOTAL}${NC} new commit(s):"
echo ""

# Build commit list
COMMIT_LIST_TG=""
COUNT=0
for commit in "${COMMITS[@]}"; do
    COUNT=$((COUNT + 1))
    MSG=$(git log -1 --format='%s' "$commit")
    AUTHOR=$(git log -1 --format='%an' "$commit")
    DATE=$(git log -1 --format='%cr' "$commit")

    printf "  ${YELLOW}%3d.${NC} ${DIM}%s${NC} %s ${DIM}(%s, %s)${NC}\n" \
        "$COUNT" "${commit:0:10}" "$MSG" "$AUTHOR" "$DATE"

    if [ "$TG_ENABLED" = "true" ]; then
        ESCAPED=$(tg_escape "$MSG")
        COMMIT_LIST_TG="${COMMIT_LIST_TG}
${COUNT}. <code>${commit:0:10}</code> ${ESCAPED}"
    fi
done
echo ""

tg_send_message "📦 <b>Changes Detected!</b>

Found <b>${TOTAL}</b> new commit(s) to merge:
${COMMIT_LIST_TG}

🔀 Starting cherry-pick..." > /dev/null 2>&1

# ── Dry Run Exit ───────────────────────────────────────────────────────────
if [ "$DRY_RUN" = "true" ]; then
    step "Dry Run Complete"
    warn "No changes were made."

    tg_send_message "🏁 <b>Dry Run Complete</b>

Found ${TOTAL} commit(s) that would be cherry-picked.
No changes were made.

Re-run without dry-run to apply." > /dev/null 2>&1

    exit 0
fi

# ── Step 5: Cherry-Pick ───────────────────────────────────────────────────
step "Step 5: Cherry-Picking Commits"

APPLIED=0
SKIPPED=0

for commit in "${COMMITS[@]}"; do
    APPLIED=$((APPLIED + 1))
    MSG=$(git log -1 --format='%s' "$commit")

    printf "${CYAN}[%d/%d]${NC} Cherry-picking: ${DIM}%s${NC} %s\n" \
        "$APPLIED" "$TOTAL" "${commit:0:10}" "$MSG"

    if ! git cherry-pick "$commit" 2>/dev/null; then
        # Check if empty (already applied)
        if git diff --cached --quiet 2>/dev/null; then
            warn "Skipping empty/already-applied: ${commit:0:10}"
            git cherry-pick --skip 2>/dev/null || true
            SKIPPED=$((SKIPPED + 1))
            APPLIED=$((APPLIED - 1))
        else
            # ── Real Conflict ───────────────────────────────────────
            ESCAPED_CONFLICT_MSG=""
            [ "$TG_ENABLED" = "true" ] && ESCAPED_CONFLICT_MSG=$(tg_escape "$MSG")

            error "Cherry-pick CONFLICT!"
            echo -e "  ${DIM}Hash:${NC}    ${commit:0:10}"
            echo -e "  ${DIM}Message:${NC} ${MSG}"
            divider

            CONFLICT_FILES=$(git diff --name-only --diff-filter=U 2>/dev/null | head -10)
            ESCAPED_FILES=""
            [ "$TG_ENABLED" = "true" ] && ESCAPED_FILES=$(tg_escape "$CONFLICT_FILES")

            # Abort cherry-pick so repo is clean for next run
            git cherry-pick --abort 2>/dev/null || true

            tg_send_message "❌ <b>Conflict Detected!</b>

Cherry-pick failed at commit <b>${APPLIED}/${TOTAL}</b>:
<code>${commit:0:10}</code> ${ESCAPED_CONFLICT_MSG}

<b>Conflicting files:</b>
<pre>${ESCAPED_FILES}</pre>

<b>To resolve manually:</b>
1️⃣ <code>git fetch upstream ${UPSTREAM_BRANCH}</code>
2️⃣ <code>git cherry-pick ${commit}</code>
3️⃣ Fix conflicts, then <code>git add . && git cherry-pick --continue</code>
4️⃣ Cherry-pick remaining commits
5️⃣ <code>git push origin ${LOCAL_BRANCH}</code>

📊 Progress: $((APPLIED - 1))/${TOTAL} applied, ${SKIPPED} skipped" > /dev/null 2>&1

            echo "::error::Cherry-pick conflict at commit ${commit:0:10}: ${MSG}"
            exit 1
        fi
    else
        success "Applied: ${MSG}"
    fi
done

# ── Step 6: Push to Origin ─────────────────────────────────────────────────
step "Pushing to Origin"

divider
echo -e "  ${GREEN}Applied:${NC} ${APPLIED} commit(s)"
[ "$SKIPPED" -gt 0 ] && echo -e "  ${YELLOW}Skipped:${NC} ${SKIPPED} commit(s) (already applied)"
echo -e "  ${DIM}Total:${NC}   ${TOTAL} processed"
divider

info "Pushing to origin/${LOCAL_BRANCH}..."

if git push origin "$LOCAL_BRANCH" 2>&1; then
    success "Pushed successfully! 🚀"

    if [ "$TG_ENABLED" = "true" ]; then
        NEW_HEAD=$(git rev-parse --short HEAD)
        NEW_MSG=$(git log -1 --format='%s' HEAD)
        ESCAPED_NEW=$(tg_escape "$NEW_MSG")

        tg_send_message "🚀 <b>Telegram-Stremio Sync Complete</b>

Successfully synced upstream and pushed to <code>origin/${LOCAL_BRANCH}</code>.

📊 <b>Summary:</b>
• Applied: ${APPLIED} commit(s)
• Skipped: ${SKIPPED} commit(s)

<b>Latest commit:</b>
<code>${NEW_HEAD}</code> ${ESCAPED_NEW}

✅ Repository is now up to date!" > /dev/null 2>&1
    fi
else
    error "Push failed!"

    if [ "$TG_ENABLED" = "true" ]; then
        tg_send_message "❌ <b>Push Failed</b>

Cherry-picks completed successfully, but pushing to
<code>origin/${LOCAL_BRANCH}</code> failed.

Please push manually:

<code>git push origin ${LOCAL_BRANCH}</code>" > /dev/null 2>&1
    fi

    exit 1
fi

echo ""
success "Done! 🎉"
