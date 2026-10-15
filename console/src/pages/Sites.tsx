import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { api, unwrap, idempotent } from "../api/client";
import { useApiMutation, useSites } from "../api/queries";
import { Dialog } from "../components/Dialog";
import {
  Card,
  DataAge,
  Empty,
  ErrorState,
  Field,
  Loading,
  PageHeader,
  Status,
} from "../components/ui";
import { usePermissions, useScope } from "../lib/context";
import { slug } from "../lib/format";

export function Sites() {
  const { orgId, projectId, base } = useScope();
  const sites = useSites(projectId);
  const can = usePermissions(orgId);
  const [open, setOpen] = useState(false);

  return (
    <>
      <PageHeader
        title="Sites"
        description="A site is a place where robots and the GPU servers that serve them share a local network. Each site keeps running on its committed configuration when the cloud is unreachable."
        actions={
          can.admin ? (
            <button type="button" className="button primary" onClick={() => setOpen(true)}>
              Add a site
            </button>
          ) : null
        }
      />
      <Card flush>
        {sites.isPending ? (
          <Loading />
        ) : sites.isError ? (
          <ErrorState error={sites.error} />
        ) : sites.data.length === 0 ? (
          <Empty title="No sites yet">
            <p>Add the site where your robots work, then connect its GPU server.</p>
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <caption className="visually-hidden">Sites</caption>
              <thead>
                <tr>
                  <th scope="col">Site</th>
                  <th scope="col">State</th>
                  <th scope="col">Agent</th>
                  <th scope="col">Last report</th>
                  <th scope="col" className="num">
                    Offline allowance
                  </th>
                </tr>
              </thead>
              <tbody>
                {sites.data.map((site) => (
                  <tr key={site.metadata.id}>
                    <td>
                      <Link to={`${base}/sites/${site.metadata.id}`}>{site.spec.display_name}</Link>
                      <div className="muted small mono">{site.spec.name}</div>
                    </td>
                    <td>
                      <Status phase={site.status.phase} />
                    </td>
                    <td>
                      {site.status.agent_version ?? <span className="muted">Not enrolled</span>}
                    </td>
                    <td>
                      <DataAge
                        ageSeconds={site.status.data_age_seconds}
                        fetchedAt={sites.dataUpdatedAt}
                        never="Never connected"
                      />
                    </td>
                    <td className="num">{site.spec.offline_authorization_hours} h</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <NewSiteDialog open={open} onClose={() => setOpen(false)} projectId={projectId} base={base} />
    </>
  );
}

function NewSiteDialog({
  open,
  onClose,
  projectId,
  base,
}: {
  open: boolean;
  onClose: () => void;
  projectId: string;
  base: string;
}) {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [hours, setHours] = useState(24);
  const create = useApiMutation(
    async () =>
      unwrap(
        await api.POST("/v1/projects/{project_id}/sites", {
          params: { path: { project_id: projectId }, header: idempotent() },
          body: {
            spec: {
              name: slug(name) || "site",
              display_name: name.trim(),
              offline_authorization_hours: hours,
            },
          },
        }),
      ),
    [["p", projectId, "sites"]],
  );
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Add a site"
      description="You will connect its GPU server next."
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate(undefined, {
            onSuccess: (site) => {
              onClose();
              void navigate(`${base}/sites/${site.metadata.id}?connect=1`);
            },
          });
        }}
      >
        <div className="dialog-body">
          <Field id="site-name" label="Site name" hint="For example, the warehouse or cell name.">
            <input
              id="site-name"
              type="text"
              required
              maxLength={200}
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </Field>
          <Field
            id="site-hours"
            label="Offline allowance (hours)"
            hint="How long the site keeps operating on its committed configuration without reaching Neura. After that it stops serving until it reconnects."
          >
            <input
              id="site-hours"
              type="number"
              min={1}
              max={8760}
              required
              value={hours}
              onChange={(event) => setHours(Number(event.target.value))}
            />
          </Field>
          {create.isError ? (
            <ErrorState error={create.error} title="Could not add the site" />
          ) : null}
        </div>
        <div className="dialog-footer form-actions">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="button primary"
            disabled={!name.trim() || create.isPending}
          >
            {create.isPending ? "Adding…" : "Add site"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
