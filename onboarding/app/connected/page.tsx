import Link from "next/link";

export default function Connected() {
  return (
    <main className="connected-page">
      <div className="connected-card">
        <div className="success-mark">Connected</div>
        <h1>Google approved. You can close this tab.</h1>
        <p>
          Your setup command is waiting on the first tab, under Step 2. Copy it
          and paste it to your agent. If Google expires the test authorization
          later, your agent will ask you to reconnect.
        </p>
        <Link className="primary-link" href="/">
          Take me back to it
        </Link>
      </div>
    </main>
  );
}
