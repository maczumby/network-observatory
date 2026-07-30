import Link from "next/link";

export default function Connected() {
  return (
    <main className="connected-page">
      <div className="connected-card">
        <div className="success-mark">Connected</div>
        <h1>Google handed you back to the Observatory.</h1>
        <p>
          Return to the setup tab, copy your private Hermes configuration, and
          test the connection. If Google expires the test authorization later,
          your agent will ask you to reconnect.
        </p>
        <Link className="primary-link" href="/">
          Return to setup
        </Link>
      </div>
    </main>
  );
}
