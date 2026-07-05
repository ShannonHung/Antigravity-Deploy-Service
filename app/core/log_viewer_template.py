"""
app/core/log_viewer_template.py

Premium HTML/CSS/JS template for GitLab job log viewing.
Supports light and dark mode with toggle, and overrides hard-to-read ANSI blue.
"""

LOG_VIEWER_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        /* CSS Variables - Default (Dark Theme) */
        :root {{
            --bg-color: #0d1117;
            --header-bg: #161b22;
            --text-color: #c9d1d9;
            --accent-color: #58a6ff;
            --border-color: #30363d;
            --line-number-color: #484f58;
            --success-color: #238636;
            --hover-bg: rgba(255, 255, 255, 0.04);
            /* Override Ansi2HTML hardcoded colors for dark mode readability */
            --ansi-black: #484f58;       /* Map pure black to gray */
            --ansi-red: #ff7b72;
            --ansi-green: #3fb950;
            --ansi-yellow: #d29922;
            --ansi-blue: #58a6ff;
            --ansi-magenta: #bc8cff;
            --ansi-cyan: #39c5cf;
            --ansi-white: #b1bac4;
            --ansi-bright-black: #8b949e;
        }}

        /* Light Theme overrides */
        [data-theme="light"] {{
            --bg-color: #ffffff;
            --header-bg: #f6f8fa;
            --text-color: #24292f;
            --accent-color: #0969da;
            --border-color: #d0d7de;
            --line-number-color: #8c959f;
            --success-color: #2da44e;
            --hover-bg: rgba(0, 0, 0, 0.04);
            /* Light theme ANSI colors */
            --ansi-black: #24292f;
            --ansi-red: #cf222e;
            --ansi-green: #116329;
            --ansi-yellow: #9a6700;
            --ansi-blue: #0969da;
            --ansi-magenta: #8250df;
            --ansi-cyan: #1b7c83;
            --ansi-white: #6e7781;
            --ansi-bright-black: #57606a;
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}

        body {{
            background-color: var(--bg-color);
            color: var(--text-color);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            height: 100vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            transition: background-color 0.2s, color 0.2s;
        }}

        header {{
            background-color: var(--header-bg);
            border-bottom: 1px solid var(--border-color);
            padding: 10px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            z-index: 100;
            transition: background-color 0.2s, border-color 0.2s;
        }}

        .title-group {{ display: flex; align-items: center; gap: 12px; }}
        .badge {{
            background: var(--accent-color);
            color: white;
            padding: 3px 10px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
        }}
        h1 {{ font-size: 16px; font-weight: 600; }}

        .controls {{ display: flex; gap: 16px; align-items: center; }}
        
        .theme-toggle {{
            background: none;
            border: 1px solid var(--border-color);
            color: var(--text-color);
            padding: 4px 10px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 500;
            display: flex;
            align-items: center;
            gap: 6px;
            transition: all 0.2s;
        }}
        .theme-toggle:hover {{
            background: var(--hover-bg);
            border-color: var(--line-number-color);
        }}

        .status {{ font-size: 13px; display: flex; align-items: center; gap: 10px; }}
        #sync-status {{ color: var(--text-color); font-weight: 500; font-size: 12px; }}
        .dot {{ width: 8px; height: 8px; background: var(--success-color); border-radius: 50%; display: inline-block; animation: pulse 2s infinite; }}
        .job-status-badge {{ background: var(--hover-bg); border: 1px solid var(--border-color); padding: 3px 10px; border-radius: 12px; font-size: 11px; color: var(--text-color); font-weight: 600; text-transform: uppercase; }}
        @keyframes pulse {{ 0%, 100% {{ opacity: 0.4; }} 50% {{ opacity: 1; }} }}

        #log-container {{
            flex: 1;
            overflow: auto;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
            font-size: 13px;
            background: var(--bg-color);
            padding-bottom: 20px;
        }}
        .log-table a {{ color: var(--ansi-blue); text-decoration: none; }}
        .log-table a:hover {{ text-decoration: underline; }}
        .log-table {{ display: table; width: 100%; border-collapse: collapse; }}
        .log-line {{ display: table-row; line-height: 1.5; }}
        .log-line:hover {{ background: var(--hover-bg); }}

        .cell {{ display: table-cell; padding: 2px 4px; vertical-align: top; border-bottom: none; }}
        .line-num {{
            color: var(--line-number-color);
            text-align: right;
            width: 50px;
            min-width: 50px;
            border-right: 1px solid var(--border-color);
            user-select: none;
            padding-right: 10px;
            padding-left: 10px;
        }}
        .content {{ padding-left: 12px; width: 100%; white-space: pre-wrap; word-break: break-all; }}

        /* Scrollbar */
        ::-webkit-scrollbar {{ width: 12px; height: 12px; }}
        ::-webkit-scrollbar-track {{ background: var(--bg-color); }}
        ::-webkit-scrollbar-thumb {{ background: var(--border-color); border-radius: 6px; border: 3px solid var(--bg-color); }}
        ::-webkit-scrollbar-thumb:hover {{ background: var(--line-number-color); }}

        /* Ansi2HTML dynamically overrides using inline styles, but we can target specific classes if we use full=False without inline=True. Since we use inline=True, we use JS to replace the hard-to-read blue. */
        .ansi-override-blue {{ color: var(--ansi-blue) !important; }}

        /* Fatal-error panel shown after MAX_FAILURES consecutive poll failures. */
        .error-panel {{
            max-width: 680px;
            margin: 60px auto;
            padding: 24px 28px;
            border: 1px solid var(--border-color);
            border-radius: 8px;
            background: var(--header-bg);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            line-height: 1.6;
        }}
        .error-panel h2 {{
            font-size: 18px;
            font-weight: 600;
            margin-bottom: 12px;
            color: #cf222e;
        }}
        .error-panel p {{ margin: 8px 0; font-size: 14px; }}
        .error-panel .error-meta {{
            margin: 16px 0;
            padding: 12px 16px;
            background: var(--bg-color);
            border-radius: 6px;
            border: 1px solid var(--border-color);
        }}
        .error-panel .error-meta > div {{
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 4px 0;
        }}
        .error-panel .error-meta .label {{
            min-width: 90px;
            font-size: 12px;
            text-transform: uppercase;
            color: var(--line-number-color);
            font-weight: 600;
        }}
        .error-panel code {{
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 13px;
            color: var(--text-color);
        }}
        .error-link {{
            color: var(--accent-color);
            word-break: break-all;
        }}
        .error-link:hover {{ text-decoration: underline; }}

        /* Soft-cap warning banner shown above the log when trace size
           crosses GITLAB_TRACE_SOFT_CAP_BYTES. Polling continues.
           Sits between <header> and #log-container as a flex sibling so
           it stays pinned at the top of the viewport and does not scroll
           with the log body. */
        .size-warning-banner {{
            flex-shrink: 0;
            padding: 12px 20px;
            border-bottom: 1px solid var(--border-color);
            border-left: 4px solid #d29922;
            background: var(--header-bg);
            font-size: 13px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            z-index: 99;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }}
        .size-warning-banner .banner-text {{ flex: 1; }}
        .size-warning-banner strong {{ color: var(--text-color); }}
        .size-warning-banner a {{
            color: var(--accent-color);
            font-weight: 600;
            white-space: nowrap;
            text-decoration: none;
        }}
        .size-warning-banner a:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <header>
        <div class="title-group">
            <span class="badge">Trace</span>
            <h1>{heading}</h1>
        </div>
        <div class="controls">
            <button class="theme-toggle" id="theme-btn" onclick="toggleTheme()">
                <span id="theme-icon">☀️</span> Light Mode
            </button>
            <div class="status">
                <span id="job-status" class="job-status-badge">...</span>
                <span id="trace-size" class="job-status-badge" title="Current log size">— B</span>
                <span class="dot" id="polling-dot"></span>
                <span id="sync-status">Connecting...</span>
                <span id="interval-status" style="color: var(--text-color); background: var(--hover-bg); padding: 2px 6px; border-radius: 6px; font-weight: 600; font-size: 11px; margin-left: 5px; border: 1px solid var(--border-color);">(5s)</span>
            </div>
        </div>
    </header>

    <div id="log-container">
        <div class="log-table" id="log-table"></div>
    </div>

    <script>
        const table = document.getElementById('log-table');
        const container = document.getElementById('log-container');
        const syncStatus = document.getElementById('sync-status');
        const intervalStatus = document.getElementById('interval-status');
        const jobStatusBadge = document.getElementById('job-status');
        const traceSizeBadge = document.getElementById('trace-size');
        const themeBtn = document.getElementById('theme-btn');
        const themeIcon = document.getElementById('theme-icon');
        const pollingDot = document.getElementById('polling-dot');
        
        let currentByteOffset = 0;
        let currentLineNum = 1;
        let timer = null;
        let isDarkMode = true;
        let currentInterval = 5000;
        let consecutiveFailures = 0;
        let sizeWarningShown = false;
        const MAX_INTERVAL = 30000;
        const MAX_FAILURES = 5;

        const TERMINAL_STATUSES = {terminal_statuses_json};

        // Server-rendered metadata rows (project/job/command identifiers and
        // any "open externally" link). Empty string for viewers with no
        // external surface (e.g. the command viewer).
        const META_HTML = `{meta_html}`;

        function formatBytes(n) {{
            if (n >= 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + ' MB';
            if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
            return n + ' B';
        }}

        function escapeHtml(s) {{
            return String(s)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        }}

        // Apply theme on load
        if (localStorage.getItem('theme') === 'light') {{
            setTheme('light');
        }}

        function toggleTheme() {{
            if (isDarkMode) {{
                setTheme('light');
            }} else {{
                setTheme('dark');
            }}
        }}

        function setTheme(theme) {{
            if (theme === 'light') {{
                document.documentElement.setAttribute('data-theme', 'light');
                themeIcon.innerText = '🌙';
                themeBtn.innerHTML = '<span id="theme-icon">🌙</span> Dark Mode';
                isDarkMode = false;
                localStorage.setItem('theme', 'light');
            }} else {{
                document.documentElement.removeAttribute('data-theme');
                themeIcon.innerText = '☀️';
                themeBtn.innerHTML = '<span id="theme-icon">☀️</span> Light Mode';
                isDarkMode = true;
                localStorage.setItem('theme', 'dark');
            }}
        }}

        function showSizeWarning(totalSize) {{
            // One-shot — render the banner above the log the first time the
            // soft cap is crossed, then leave it in place. Polling continues.
            if (sizeWarningShown) return;
            sizeWarningShown = true;
            const banner = document.createElement('div');
            banner.className = 'size-warning-banner';
            banner.innerHTML = `
                <div class="banner-text">
                    <strong>Log is large (${{formatBytes(totalSize)}}).</strong>
                    Rendering many lines may slow your browser.
                </div>
            `;
            // Insert as a flex sibling between <header> and #log-container
            // so the banner stays pinned at the top of the viewport.
            container.parentNode.insertBefore(banner, container);
        }}

        function showFatalError(opts) {{
            // ``opts`` = {{ reason: 'poll_failed' | 'too_large',
            //              err?: Error, totalSize?: number }}
            // Stop polling permanently.
            if (timer) clearTimeout(timer);
            timer = null;

            // Update status indicators.
            pollingDot.style.animation = 'none';
            pollingDot.style.background = '#cf222e';
            pollingDot.style.opacity = '1';
            syncStatus.innerText = 'Stopped';
            intervalStatus.style.display = 'none';

            let heading;
            let body;
            let extraRows;
            if (opts.reason === 'too_large') {{
                jobStatusBadge.innerText = 'TOO LARGE';
                heading = 'Log too large to display';
                body = `This log has reached ${{formatBytes(opts.totalSize)}}, which exceeds the viewer's hard cap. Rendering it in the browser would make the page unresponsive. You can still read the full log directly on the control node:`;
                extraRows = `<div><span class="label">Trace size</span><code>${{formatBytes(opts.totalSize)}}</code></div>`;

                // Where the full log lives — host/port/user/path, plus a
                // copy-pasteable ssh + tail. Guard each field so a partial
                // response still renders what it can.
                if (opts.logHost) {{
                    extraRows += `<div><span class="label">Host</span><code>${{escapeHtml(opts.logHost)}}${{opts.logPort ? ':' + opts.logPort : ''}}</code></div>`;
                }}
                if (opts.logFilePath) {{
                    extraRows += `<div><span class="label">File</span><code>${{escapeHtml(opts.logFilePath)}}</code></div>`;
                }}
                if (opts.logHost && opts.logFilePath) {{
                    const user = opts.logUser ? escapeHtml(opts.logUser) + '@' : '';
                    const port = opts.logPort ? ' -p ' + opts.logPort : '';
                    const sshCmd = `ssh ${{user}}${{escapeHtml(opts.logHost)}}${{port}} tail -f ${{escapeHtml(opts.logFilePath)}}`;
                    extraRows += `<div><span class="label">Read it</span><code>${{sshCmd}}</code></div>`;
                }}
            }} else if (opts.reason === 'not_logged') {{
                jobStatusBadge.innerText = 'NOT LOGGED';
                heading = 'Logging not enabled for this command';
                body = `This command was not configured with <code>logged: true</code>, so no run log is streamed to the viewer. Its output is returned directly in the command execution API response instead.`;
                extraRows = '';
            }} else {{
                jobStatusBadge.innerText = 'UNAVAILABLE';
                const detail = opts.err && opts.err.message ? opts.err.message : 'Unknown error';
                heading = 'Cannot load logs';
                body = `Failed to fetch logs after ${{MAX_FAILURES}} consecutive attempts. Polling has stopped to avoid further load.`;
                extraRows = `<div><span class="label">Last error</span><code>${{detail}}</code></div>`;
            }}

            // Append the panel below whatever's already rendered (so the
            // user keeps any lines they were reading when the cap hit).
            const panel = document.createElement('div');
            panel.className = 'error-panel';
            panel.innerHTML = `
                <h2>${{heading}}</h2>
                <p>${{body}}</p>
                <div class="error-meta">${{META_HTML}}${{extraRows}}</div>
            `;
            container.appendChild(panel);
        }}

        async function refresh() {{
            try {{
                const sep = `{trace_url}`.includes('?') ? '&' : '?';
                const res = await fetch(`{trace_url}${{sep}}byte_offset=${{currentByteOffset}}&line_num=${{currentLineNum}}&t=${{Date.now()}}`, {{ cache: 'no-store' }});
                // Not logged in (no auth cookie/header): send the browser to the
                // login page and come straight back to this viewer afterwards.
                if (res.status === 401 || res.status === 403) {{
                    if (timer) clearTimeout(timer);
                    timer = null;
                    window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname + window.location.search);
                    return;
                }}
                if (!res.ok) throw new Error(`HTTP ${{res.status}}`);
                const json = await res.json();
                if (!json.data || !json.data.lines) throw new Error("Invalid API response");

                consecutiveFailures = 0;
                const data = json.data;
                const status = data.status || 'unknown';
                jobStatusBadge.innerText = status.toUpperCase();
                if (typeof data.total_size === 'number') {{
                    traceSizeBadge.innerText = formatBytes(data.total_size);
                }}

                // Command wasn't run with logged:true — there is no run log to
                // stream. Show a one-shot notice and stop polling (a log file
                // will never appear), rather than spinning on an empty page.
                if (data.not_logged) {{
                    showFatalError({{ reason: 'not_logged' }});
                    return;
                }}

                // Hard cap: stop polling, switch to error panel, keep
                // already-rendered lines visible. Bail before touching
                // status badges so the panel's TOO LARGE label sticks.
                if (data.too_large) {{
                    showFatalError({{
                        reason: 'too_large',
                        totalSize: data.total_size,
                        logHost: data.log_host,
                        logPort: data.log_port,
                        logUser: data.log_user,
                        logFilePath: data.log_file_path,
                    }});
                    return;
                }}

                // Soft cap: one-shot banner, keep polling normally.
                if (data.size_warning) {{
                    showSizeWarning(data.total_size);
                }}

                if (TERMINAL_STATUSES.includes(status)) {{
                    if (timer) clearTimeout(timer);
                    timer = null;
                    pollingDot.style.animation = 'none';
                    pollingDot.style.opacity = '0.5';
                    syncStatus.innerText = `Finished`;
                    intervalStatus.style.display = 'none';
                }} else {{
                    syncStatus.innerText = `Syncing...`;
                    intervalStatus.style.display = 'inline';
                }}

                // Always advance the byte cursor (server may bump it even with
                // zero lines, e.g. when only whitespace was added).
                currentByteOffset = data.next_byte_offset;
                currentLineNum = data.next_line_num;

                if (data.lines.length === 0) {{
                    // No new logs, backoff polling up to MAX_INTERVAL
                    currentInterval = Math.min(currentInterval + 5000, MAX_INTERVAL);
                }} else {{
                    // New logs found, reset to fast polling
                    currentInterval = 5000;
                    render(data.lines);

                    // Auto-scroll logic (always on since we removed toggles)
                    container.scrollTop = container.scrollHeight;
                }}
                
                // Update the UI showing current interval frequency
                intervalStatus.innerText = `(${{currentInterval / 1000}}s)`;
                
            }} catch (e) {{
                consecutiveFailures += 1;
                console.error('Log poll failed', e);
                if (consecutiveFailures >= MAX_FAILURES) {{
                    showFatalError({{ reason: 'poll_failed', err: e }});
                    // showFatalError sets timer=null so the finally block
                    // won't reschedule.
                }} else {{
                    syncStatus.innerText = `Sync Failed (${{consecutiveFailures}}/${{MAX_FAILURES}})`;
                }}
            }} finally {{
                // Schedule next poll if job is not finished
                if (timer !== null) {{
                    timer = setTimeout(refresh, currentInterval);
                }}
            }}
        }}

        function render(lines) {{
            let html = '';
            lines.forEach(l => {{
                // Replace Ansi2HTML hardcoded hex colors with our themeable CSS variables
                let content = l.content_html
                    .replace(/color: #000000/gi, 'color: var(--ansi-black)')
                    .replace(/color: #aa0000/gi, 'color: var(--ansi-red)')
                    .replace(/color: #00aa00/gi, 'color: var(--ansi-green)')
                    .replace(/color: #aaaa00/gi, 'color: var(--ansi-yellow)')
                    .replace(/color: #0000aa/gi, 'color: var(--ansi-blue)')
                    .replace(/color: #aa00aa/gi, 'color: var(--ansi-magenta)')
                    .replace(/color: #00aaaa/gi, 'color: var(--ansi-cyan)')
                    .replace(/color: #aaaaaa/gi, 'color: var(--ansi-white)')
                    .replace(/color: #555555/gi, 'color: var(--ansi-bright-black)');
                
                html += `
                    <div class="log-line">
                        <div class="cell line-num">${{l.num}}</div>
                        <div class="cell content">${{content}}</div>
                    </div>
                `;
            }});
            table.insertAdjacentHTML('beforeend', html);
        }}

        // Initial setup
        timer = setTimeout(refresh, 0); // Start immediately
    </script>
</body>
</html>
"""
