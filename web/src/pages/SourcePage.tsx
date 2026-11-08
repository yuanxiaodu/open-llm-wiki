import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { apiGet, errText } from "../api.ts";
import { MarkdownBody } from "../components/MarkdownBody.tsx";

export function SourcePage() {
  const [params] = useSearchParams();
  const path = params.get("path") ?? "";
  const [body, setBody] = useState("");
  const [metadata, setMetadata] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  useEffect(() => {
    let alive = true;
    setBody(""); setError("");
    apiGet<{ body_md: string; metadata: Record<string, string> }>(`/api/wiki/source?path=${encodeURIComponent(path)}`)
      .then((data) => { if (alive) { setBody(data.body_md); setMetadata(data.metadata); } })
      .catch((e) => { if (alive) setError(errText(e, "来源没取到")); });
    return () => { alive = false; };
  }, [path]);
  return <div className="vault-scroll"><article className="vault-index">
    <h1>{metadata.source_file || "来源正文"}</h1><p>{metadata.ingested && `归档于 ${metadata.ingested}`}</p>
    {/^https?:\/\//i.test(metadata.source_url || "") && <a href={metadata.source_url} target="_blank" rel="noreferrer">查看原始网页</a>}
    {error ? <p role="alert">{error}</p> : <MarkdownBody markdown={body} />}
  </article></div>;
}
