import { describe, expect, it } from "vitest";
import { analysisMetadata, normalizeWikiDetail } from "./wiki.ts";

describe("analysis publication and evidence", () => {
  it("keeps draft and dispute signals for readers", () => {
    const page = normalizeWikiDetail({ path: "queries/test.md", metadata: { status: "draft" }, contested: true, contradictions: ["other"] });
    expect(page.status).toBe("draft"); expect(page.contested).toBe(true); expect(page.contradictions).toEqual(["other"]);
  });
  it("decodes basis revisions and paragraph provenance without trusting malformed metadata", () => {
    const data = analysisMetadata({ analysis: JSON.stringify({ question: "问题", scope: "条件", paragraph_sources: [{ paragraph: "结论一", sources: ["raw/a.md"] }] }), basis_versions: JSON.stringify([{ path: "concepts/a.md", head: "abc123" }]) });
    expect(data.question).toBe("问题"); expect(data.basis[0]?.head).toBe("abc123");
    expect(data.paragraphs[0]?.sources).toEqual(["raw/a.md"]);
    expect(analysisMetadata({ analysis: "{", basis_versions: "invalid" }).basis).toEqual([]);
  });
});
