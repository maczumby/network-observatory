import Link from "next/link";

export default function Connected() {
  return (
    <main className="connected-page">
      <div className="connected-card">
        <div className="success-mark">Connected</div>
        <h1>Google approved. You're done.</h1>
        <p>
          If you already pasted the setup command to your agent, ask it to test
          the connection; everything works now. If you skipped that step, it's
          waiting on the setup tab. If Google expires the test authorization
          later, your agent will hand you a reconnect link.
        </p>
        <Link className="primary-link" href="/">
          Back to setup
        </Link>
      </div>
    </main>
  );
}
