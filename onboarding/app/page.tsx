"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";

type Setup = {
  connectUrl: string;
  mcpUrl: string;
  hermesCommand: string | null;
  hermesConfig: string | null;
  note: string;
};

export default function Home() {
  const [email, setEmail] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [setup, setSetup] = useState<Setup | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const hermesSetup = useMemo(
    () => setup?.hermesCommand || setup?.hermesConfig || "",
    [setup],
  );

  // The Google consent flow leaves this page, and the setup command is shown
  // exactly once (the server keeps only a hash). Losing it on "back" strands
  // the user, so keep it for the life of this browser tab.
  useEffect(() => {
    const saved = sessionStorage.getItem("netobs-setup");
    if (saved) {
      try {
        setSetup(JSON.parse(saved) as Setup);
      } catch {
        sessionStorage.removeItem("netobs-setup");
      }
    }
  }, []);

  function startOver() {
    sessionStorage.removeItem("netobs-setup");
    setSetup(null);
    setEmail("");
    setInviteCode("");
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setSetup(null);
    try {
      const response = await fetch("/api/provision", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ email, inviteCode }),
      });
      const data = (await response.json()) as Setup & { error?: string };
      if (!response.ok) throw new Error(data.error || "Setup failed.");
      sessionStorage.setItem("netobs-setup", JSON.stringify(data));
      setSetup(data);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Setup failed.");
    } finally {
      setBusy(false);
    }
  }

  async function copySetup() {
    if (hermesSetup) await navigator.clipboard.writeText(hermesSetup);
  }

  return (
    <main>
      <header className="topbar">
        <a className="wordmark" href="https://github.com/maczumby/network-observatory">
          Network Observatory
        </a>
        <span className="status">Private enrichment setup</span>
      </header>

      <section className="hero">
        <div className="eyebrow">Your network, with a little more memory</div>
        <h1>Connect Gmail without handing over your inbox.</h1>
        <p className="lede">
          Your LinkedIn map already works on its own. This optional connection lets
          your Hermes agent see who you exchanged email with and when. Message bodies
          and attachments stay out of the Observatory.
        </p>
      </section>

      <section className="setup-grid" aria-label="Gmail enrichment setup">
        <div className="steps">
          <div className="step">
            <span>01</span>
            <div>
              <h2>Sign in</h2>
              <p>
                Enter the Gmail you want connected. You need to be on the tester
                list first; if you're not sure, you probably are.
              </p>
            </div>
          </div>
          <div className="step">
            <span>02</span>
            <div>
              <h2>Approve Google</h2>
              <p>
                The test app is currently unverified, so Google shows a warning.
                That's expected. If Google says access is denied, you're not on
                the tester list yet: send Mari the exact Gmail you used, then
                retry this page. While the app is in testing, you'll reconnect
                every seven days.
              </p>
            </div>
          </div>
          <div className="step">
            <span>03</span>
            <div>
              <h2>Give Hermes the endpoint</h2>
              <p>Your private MCP address connects only your agent and your Gmail.</p>
            </div>
          </div>
        </div>

        <div className="panel">
          {!setup ? (
            <form onSubmit={submit}>
              <label htmlFor="email">Google account email</label>
              <input
                id="email"
                type="email"
                autoComplete="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="you@example.com"
                required
              />

              <label htmlFor="invite">Invite code (only if you were given one)</label>
              <input
                id="invite"
                type="password"
                autoComplete="one-time-code"
                value={inviteCode}
                onChange={(event) => setInviteCode(event.target.value)}
                placeholder="netobs_… or leave blank"
              />

              {error ? <p className="error" role="alert">{error}</p> : null}
              <button type="submit" disabled={busy}>
                {busy ? "Creating your private session…" : "Create my connection"}
              </button>
              <p className="fineprint">
                Your email is converted to a private identifier. The onboarding
                service does not store it in plain text.
              </p>
            </form>
          ) : (
            <div className="success" aria-live="polite">
              <div className="success-mark">Ready</div>
              <h2>Two steps, in order.</h2>
              <ol className="success-steps">
                <li>
                  <a
                    className="primary-link"
                    href={setup.connectUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Step 1 — Connect my Google account
                  </a>
                  <p>
                    Opens in a new tab. Approve the Google screen there, then
                    come back to this tab. This page will still be here.
                  </p>
                </li>
                <li>
                  <div className="code-wrap">
                    <div className="code-label">Step 2 — Paste this to your agent</div>
                    <pre>{hermesSetup}</pre>
                    <button className="secondary" type="button" onClick={copySetup}>
                      Copy setup
                    </button>
                  </div>
                  <p>
                    Copy it now. For your security this command is shown only
                    once; if you lose it, start over and sign in again.
                  </p>
                </li>
              </ol>
              <p className="fineprint">{setup.note}</p>
              <button className="linklike" type="button" onClick={startOver}>
                Start over with a different account
              </button>
            </div>
          )}
        </div>
      </section>

      <section className="trust">
        <div>
          <strong>LinkedIn remains the source of truth.</strong>
          <span>Gmail is optional enrichment, never a requirement.</span>
        </div>
        <div>
          <strong>Identity decisions stay reversible.</strong>
          <span>Potential duplicates wait for human confirmation.</span>
        </div>
        <div>
          <strong>Your inbox is not a knowledge base.</strong>
          <span>The Observatory keeps relationship timing, not correspondence.</span>
        </div>
      </section>

      <footer>
        <a href="https://github.com/maczumby/network-observatory">
          View the public source
        </a>
        <span>Built for small, trusted testing while Google verification is pending.</span>
      </footer>
    </main>
  );
}
