import Link from "next/link";

export default function Connected() {
  return (
    <main className="connected-page">
      <div className="connected-card">
        <div className="success-mark">Connected</div>
        <h1>Google approved. One more step.</h1>
        <p>
          <strong>Start a new chat with your agent.</strong> The new tools only
          appear in the next session, so testing in your current chat will look
          like it failed when it hasn&rsquo;t. In the fresh chat, ask your agent
          to test the connection.
        </p>
        <p>
          If you never pasted the setup command, it&rsquo;s still waiting on the
          setup tab &mdash; grab it before you close that tab, because it
          isn&rsquo;t shown again. If Google expires the test authorization
          later, your agent will hand you a reconnect link.
        </p>
        <Link className="primary-link" href="/">
          Back to setup
        </Link>
      </div>
    </main>
  );
}
