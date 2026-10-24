// Sign a CLI in (design §29.2): `neura login` shows a code and opens this page; a signed-in person
// checks the code and approves it for one of their workspaces. The CLI never sees their
// credentials, and the code is worth nothing without the secret only the CLI holds.
import { useId, useState } from "react";
import { useSearchParams } from "react-router";

import { api, idempotent, unwrap } from "../api/client";
import { useApiMutation, useMe } from "../api/queries";
import { TerminalIcon } from "../components/Icons";
import { Callout, Card, ErrorState, Field, Loading, PageHeader } from "../components/ui";

const CODE = /^[A-Z0-9]{4}-?[A-Z0-9]{4}$/;

export function Activate() {
  const [params] = useSearchParams();
  const me = useMe();
  const [code, setCode] = useState((params.get("code") ?? "").toUpperCase());
  const [org, setOrg] = useState("");
  const codeId = useId();
  const orgId = useId();
  const approve = useApiMutation(async (organization: string) =>
    unwrap(
      await api.POST("/v1/login-approvals", {
        params: { header: idempotent() },
        body: {
          user_code: code.trim().toUpperCase(),
          ...(organization ? { organization_id: organization } : {}),
        },
      }),
    ),
  );
  if (me.isPending) return <Loading />;
  if (me.isError) return <ErrorState error={me.error} />;
  const organizations = me.data.organizations;
  const chosen = org || organizations[0]?.id || "";
  const valid = CODE.test(code.trim().toUpperCase());

  return (
    <main className="narrow stack" style={{ maxWidth: 520, margin: "48px auto", padding: 16 }}>
      <PageHeader
        title="Sign in the command line"
        icon={<TerminalIcon />}
        description="Approve only a code you see in your own terminal, from a `neura login` you just ran."
      />
      {approve.data ? (
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
              approve.mutate(chosen);
            }}
          >
            <Field id={codeId} label="Code" hint="As the terminal shows it, like BCDF-2345">
              <input
                id={codeId}
                value={code}
                onChange={(e) => setCode(e.target.value.toUpperCase())}
                aria-describedby={`${codeId}-hint`}
                autoComplete="off"
                spellCheck={false}
                required
              />
            </Field>
            {organizations.length > 1 ? (
              <Field id={orgId} label="Workspace">
                <select id={orgId} value={chosen} onChange={(e) => setOrg(e.target.value)}>
                  {organizations.map((o) => (
                    <option key={o.id} value={o.id}>
                      {o.display_name}
                    </option>
                  ))}
                </select>
              </Field>
            ) : null}
            {approve.isError ? <ErrorState error={approve.error} /> : null}
            <button type="submit" className="button primary" disabled={!valid || approve.isPending}>
              {approve.isPending ? "Approving…" : "Approve"}
            </button>
          </form>
        </Card>
      )}
    </main>
  );
}
