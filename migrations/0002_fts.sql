-- R2：共享语料的全文检索（FTS5）。
-- 只装三样：公共房间的人话、上传材料的解析文本、wiki 生效正文。
-- 私聊一个字都不进这张表——这是裁定，不是疏忽。

CREATE VIRTUAL TABLE fts_shared USING fts5(
    kind    UNINDEXED,   -- message / upload / page
    ref_id  UNINDEXED,   -- message_id / upload_files.id / wiki_pages.id
    heading,             -- 说话人 / 文件名 / 页标题
    body,
    tokenize='trigram'   -- 中文没有空格分词，trigram 才能按子串命中
);
