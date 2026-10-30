// Sign a CLI in (design §29.2): `neura login` shows a code and opens this page; a signed-in person
// types the code their terminal shows, sees what is waiting for it, and approves it for one of
// their workspaces. A link alone approves nothing: anyone can start a sign-in and send its link.
// The CLI never sees the person's credentials, and the code is worth nothing without the secret
// only the CLI holds.
import { useId, useState } from "react";
import { useSearchParams } from "react-router";

import { api, idempotent, unwrap } from "../api/client";
import { useApiMutation, useLoginCode, useMe } from "../api/queries";
import { Field } from "../components/Field";
import { TerminalIcon } from "../components/Icons";
import { Callout, Card, ErrorState, KeyValue, Loading, PageHeader, Time } from "../components/ui";

const CODE = /^[A-Z0-9]{4}-?[A-Z0-9]{4}$/;
// The token a CLI gets carries the person's highest permission in the workspace.
const HIGHEST = ["admin", "deploy", "read"] as const;

/** The code as the API writes it (upper case, with its dash), or null while it is incomplete. */
function normalized(text: string): string | null {
  const code = text.trim().toUpperCase();
  if (!CODE.test(code)) return null;
  const plain = code.replace("-", "");
  return `${plain.slice(0, 4)}-${plain.slice(4)}`;
}

export function Activate() {
  const [params] = useSearchParams();
  const linked = normalized(params.get("code") ?? "");
  const me = useMe();
  const [typed, setTyped] = useState("");
  const [org, setOrg] = useState("");
  const codeId = useId();
  const orgId = useId();
  const code = normalized(typed);
  const waiting = useLoginCode(code);
  const approve = useApiMutation(async (input: { code: string; organization: string }) =>
    unwrap(
      await api.POST("/v1/login-approvals", {
        params: { header: idempotent() },
        body: {
          user_code: input.code,
          ...(input.organization ? { organization_id: input.organization } : {}),
        },
      }),
    ),
  );
  if (me.isPending) return <Loading />;
  if (me.isError) return <ErrorState error={me.error} />;
  const organizations = me.data.organizations;
  const chosen = org || organizations[0]?.id || "";
  const workspace = organizations.find((o) => o.id === chosen);
  const access = HIGHEST.find((p) => workspace?.permissions.includes(p)) ?? "no";

  return (
    <main className="narrow stack" style={{ maxWidth: 520, margin: "48px auto", padding: 16 }}>
      <PageHeader
        title="Sign in the command line"
        icon={<TerminalIcon />}
        description="Approve only a code you see in your own terminal, from the neura login you just ran."
      />
      {me.data.kind !== "user" ? (
        <Callout tone="warn" title="Only a person can approve a sign-in">
          <p>
            You are signed in with an API token. Sign in with your organization&apos;s single
            sign-on to approve a command line&apos;s code.
          </p>
        </Callout>
      ) : approve.data ? (
        <Callout tone="ok" title="Signed in">
          <p>
            {approve.data.client_name} now has {approve.data.scopes.join(", ") || "no"} access to{" "}
            {organizations.find((o) => o.id === approve.data.organization_id)?.display_name ??
              "the workspace"}
            , for 30 days. You can close this tab.
          </p>
        </Callout>
      ) : (
        <Card>
          <form
            className="stack"
            onSubmit={(event) => {
              event.preventDefault();
              if (code && waiting.data) approve.mutate({ code, organization: chosen });
            }}
          >
            <Field
              id={codeId}
              label="Code"
              hint={
                linked ? (
                  <>
                    Type the code your terminal shows. The link you opened is for{" "}
                    <span className="mono" style={{ whiteSpace: "nowrap" }}>
                      {linked}
                    </span>
                    : they must be the same.
                  </>
                ) : (
                  "As your terminal shows it, like BCDF-2345."
                )
              }
            >
              <input
                value={typed}
                onChange={(e) => setTyped(e.target.value.toUpperCase())}
                autoComplete="off"
                spellCheck={false}
                required
              />
            </Field>
            {linked && code && code !== linked ? (
              <Callout tone="warn" title="This is not the code in the link you opened">
                <p>Approve only the code your own terminal shows.</p>
              </Callout>
            ) : null}
            {waiting.data ? (
              <div className="stack" style={{ gap: 8 }} aria-live="polite">
                <KeyValue
                  items={[
                    ["Signing in", waiting.data.client_name],
                    ["Started", <Time key="started" iso={waiting.data.created_at} />],
                    ["Expires", <Time key="expires" iso={waiting.data.expires_at} />],
                  ]}
                />
                <p className="secondary small">
                  It gets {access} access to {workspace?.display_name ?? "the workspace"}, for 30
                  days. Its name is what the command line calls itself: approve only if you just ran{" "}
                  <code>neura login</code> there.
                </p>
              </div>
            ) : waiting.isError ? (
              <ErrorState error={waiting.error} />
            ) : null}
            {organizations.length > 1 ? (
              <Field id={orgId} label="Workspace">
                <select value={chosen} onChange={(e) => setOrg(e.target.value)}>
                  {organizations.map((o) => (
                    <option key={o.id} value={o.id}>
                      {o.display_name}
                    </option>
                  ))}
                </select>
              </Field>
            ) : null}
            {approve.isError ? <ErrorState error={approve.error} /> : null}
            <button
              type="submit"
              className="button primary"
              disabled={!waiting.data || approve.isPending}
            >
              {approve.isPending ? "Approving…" : "Approve"}
            </button>
          </form>
        </Card>
      )}
    </main>
  );
}
