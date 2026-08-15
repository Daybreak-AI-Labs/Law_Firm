<script lang="ts">
  import { invoke } from '@tauri-apps/api/core';
  import { listen, type UnlistenFn } from '@tauri-apps/api/event';
  import { getCurrentWindow } from '@tauri-apps/api/window';
  import { onMount, onDestroy } from 'svelte';

  type Status = 'idle' | 'installing' | 'done' | 'failed' | 'cancelled';
  type InstallFailure = { code: string; message: string };

  const MAX_LOG_LINES = 1000;
  const MAX_LOG_CHARS = 500_000;
  const LOG_BATCH_MS = 50;

  let status: Status = 'idle';
  let lines: string[] = [];
  let errorMsg = '';
  let logEl: HTMLElement | undefined;
  let activeInstall: Promise<void> | undefined;
  let cancellationRequested = false;
  let destroyed = false;
  let listenersRegistered = false;
  let listenerRegistrationFailed = false;
  let cleanupBlocked = false;
  let resolveListenerReadiness!: (ready: boolean) => void;
  const listenerReadiness = new Promise<boolean>((resolve) => {
    resolveListenerReadiness = resolve;
  });
  let pendingLogLines: string[] = [];
  let logFlushTimer: ReturnType<typeof setTimeout> | undefined;

  const unlisteners: UnlistenFn[] = [];

  function flushLogs() {
    if (logFlushTimer !== undefined) {
      clearTimeout(logFlushTimer);
      logFlushTimer = undefined;
    }
    if (pendingLogLines.length === 0) return;

    let next = lines.concat(pendingLogLines);
    pendingLogLines = [];
    if (next.length > MAX_LOG_LINES) {
      next = next.slice(next.length - MAX_LOG_LINES);
    }
    let chars = 0;
    let first = next.length;
    while (first > 0) {
      const size = next[first - 1].length + 1;
      if (chars + size > MAX_LOG_CHARS) break;
      chars += size;
      first -= 1;
    }
    lines = first === 0 ? next : next.slice(first);
    queueMicrotask(() => logEl?.scrollTo(0, logEl.scrollHeight));
  }

  function queueLog(line: string) {
    pendingLogLines.push(line);
    if (pendingLogLines.length >= 50) {
      flushLogs();
    } else if (logFlushTimer === undefined) {
      logFlushTimer = setTimeout(flushLogs, LOG_BATCH_MS);
    }
  }

  function installFailure(error: unknown): InstallFailure {
    if (typeof error === 'object' && error !== null) {
      const value = error as { code?: unknown; message?: unknown };
      if (typeof value.code === 'string' && typeof value.message === 'string') {
        return { code: value.code, message: value.message };
      }
    }
    return { code: 'failed', message: String(error) };
  }

  async function registerListener<T>(
    event: string,
    handler: Parameters<typeof listen<T>>[1]
  ) {
    const unlisten = await listen<T>(event, handler);
    if (destroyed) {
      unlisten();
    } else {
      unlisteners.push(unlisten);
    }
  }

  onMount(() => {
    void Promise.all([
      registerListener<string>('install-log', (e) => {
        queueLog(e.payload);
      }),
      registerListener<void>('install-close-requested', () => {
        void confirmCancelAndClose();
      }),
      registerListener<void>('install-cleanup-failed-close-requested', () => {
        void confirmForceExit();
      }),
    ]).then(
      () => {
        listenersRegistered = true;
        resolveListenerReadiness(true);
      },
      (error) => {
        listenerRegistrationFailed = true;
        errorMsg = `Could not initialize installer event listeners: ${String(error)}`;
        status = 'failed';
        resolveListenerReadiness(false);
      }
    );
  });

  onDestroy(() => {
    destroyed = true;
    if (logFlushTimer !== undefined) clearTimeout(logFlushTimer);
    unlisteners.splice(0).forEach((unlisten) => unlisten());
  });

  async function startInstall() {
    if (!(await listenerReadiness)) return;
    status = 'installing';
    lines = [];
    pendingLogLines = [];
    if (logFlushTimer !== undefined) {
      clearTimeout(logFlushTimer);
      logFlushTimer = undefined;
    }
    errorMsg = '';
    cleanupBlocked = false;
    cancellationRequested = false;
    const operation = invoke<void>('install');
    activeInstall = operation;
    try {
      await operation;
      flushLogs();
      // The command response is the authoritative terminal signal. Progress
      // events are intentionally best-effort, so a lost event must never
      // leave a completed installation stuck on this screen.
      status = 'done';
    } catch (e) {
      flushLogs();
      const failure = installFailure(e);
      if (cancellationRequested && failure.code === 'cancelled') {
        status = 'cancelled';
        errorMsg = failure.message;
      } else {
        status = 'failed';
        cleanupBlocked = failure.code === 'cleanup_failed';
        errorMsg = failure.message;
      }
    } finally {
      if (activeInstall === operation) activeInstall = undefined;
    }
  }

  async function cancelInstall(): Promise<boolean> {
    if (status !== 'installing' || cancellationRequested) return false;
    cancellationRequested = true;
    const operation = activeInstall;
    try {
      await invoke('cancel_install');
    } catch (e) {
      cancellationRequested = false;
      errorMsg = `Could not cancel safely: ${String(e)}`;
      return false;
    }
    try {
      await operation;
      return false;
    } catch (error) {
      // startInstall presents the result; this return value only decides
      // whether it is safe for the close-request handler to close the window.
      return installFailure(error).code === 'cancelled';
    }
  }

  async function confirmCancelAndClose() {
    if (status !== 'installing') {
      await getCurrentWindow().close();
      return;
    }
    if (!window.confirm(
      'Installation is still running. Cancel it, stop its child processes, and close Lightwork?'
    )) {
      return;
    }
    if (await cancelInstall()) {
      await getCurrentWindow().close();
    }
  }

  async function confirmForceExit() {
    if (!window.confirm(
      'Lightwork could not verify that every installer child process stopped. Restart is blocked. Force the installer app to exit now?'
    )) {
      return;
    }
    try {
      await invoke('force_exit_after_cleanup_failure');
    } catch (error) {
      errorMsg = `Could not force the installer to exit: ${installFailure(error).message}`;
    }
  }
</script>

<main>
  <p class="sr-only" aria-live="polite">
    {status === 'installing' ? (cancellationRequested ? 'Cancelling installation' : 'Installing Lightwork') : ''}
  </p>
  <header>
    <h1>Lightwork</h1>
    <p class="sub">Authenticated source bootstrap for authorized operators.</p>
  </header>

  {#if status === 'idle'}
    <section>
      <p>
        This clones a pinned commit from the private Lightwork repository and
        installs its readable Python source. Network access and GitHub authorization
        are required. Python is installed first if needed.
      </p>
      <button disabled={!listenersRegistered} onclick={startInstall}>
        {listenersRegistered ? 'Install from authorized source' : 'Preparing installer…'}
      </button>
    </section>
  {:else if status === 'installing'}
    <section>
      <h2>Installing…</h2>
      <p class="sub">Hang tight — this can take a minute or two.</p>
      <pre bind:this={logEl} class="log">{lines.join('\n')}</pre>
      <button class="cancel" disabled={cancellationRequested} onclick={cancelInstall}>
        {cancellationRequested ? 'Cancelling…' : 'Cancel installation'}
      </button>
    </section>
  {:else if status === 'done'}
    <section class="done">
      <h2>Lightwork is installed. 🎉</h2>
      <p>Open a terminal and run <code>maverick init</code> to finish setup.</p>
      <pre class="log">{lines.join('\n')}</pre>
    </section>
  {:else if status === 'failed'}
    <section class="failed">
      <h2>Install failed</h2>
      <p class="error">{errorMsg}</p>
      <pre class="log">{lines.join('\n')}</pre>
      {#if cleanupBlocked}
        <p>
          A new install is blocked to prevent overlap with an unverified process tree.
          Review the diagnostics above, then close the app explicitly.
        </p>
        <button class="cancel" onclick={confirmForceExit}>Force close installer</button>
      {:else if listenerRegistrationFailed}
        <button onclick={() => window.location.reload()}>Reload installer</button>
      {:else}
        <button onclick={startInstall}>Try again</button>
      {/if}
    </section>
  {:else}
    <section class="cancelled">
      <h2>Installation cancelled</h2>
      <p>{errorMsg}</p>
      <pre class="log">{lines.join('\n')}</pre>
      <button onclick={startInstall}>Start again</button>
    </section>
  {/if}
</main>

<style>
  :global(body) {
    margin: 0;
    font-family: -apple-system, system-ui, sans-serif;
    background: #0d1117;
    color: #f0f6fc;
  }
  main { padding: 2rem; max-width: 720px; margin: 0 auto; }
  header { margin-bottom: 2rem; }
  h1 { font-size: 2rem; margin: 0; }
  .sub { color: #8b949e; }
  section { background: #161b22; padding: 1.5rem; border-radius: 8px; }
  h2 { margin-top: 0; font-size: 1.25rem; }
  button {
    background: #238636; color: #fff; border: none; padding: 0.7rem 1.2rem;
    border-radius: 6px; cursor: pointer; font-size: 1rem;
  }
  button:hover { background: #2ea043; }
  button:disabled {
    background: #30363d;
    cursor: wait;
    opacity: 0.75;
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
  button.cancel {
    margin-top: 1rem;
    background: #8b1a1a;
  }
  button.cancel:hover { background: #a52727; }
  code {
    background: #0d1117; border: 1px solid #30363d; border-radius: 4px;
    padding: 0.1rem 0.35rem; font-size: 0.9em;
  }
  .log {
    margin-top: 1rem; max-height: 320px; overflow: auto;
    background: #010409; border: 1px solid #30363d; border-radius: 6px;
    padding: 0.75rem; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.8rem; line-height: 1.4; white-space: pre-wrap; word-break: break-word;
    color: #c9d1d9;
  }
  .done h2 { color: #2ea043; }
  .failed h2 { color: #f85149; }
  .cancelled h2 { color: #d29922; }
  .error { color: #f85149; }
</style>
