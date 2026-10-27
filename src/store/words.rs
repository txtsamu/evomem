use std::sync::Arc;

use crate::error::Result;
use crate::text::stem;

use super::Store;

/// One vocabulary word with its stem, computed once instead of per query.
#[derive(Debug, Clone)]
pub struct VocabWord {
    pub word: String,
    pub stem: String,
}

/// One posting from the inverted index: an occurrence of an indexed word.
#[derive(Debug, Clone)]
pub struct Posting {
    pub chunk_id: i64,
    pub attr: i64,
    pub pos: i64,
}

impl Store {
    pub fn live_chunk_count(&self) -> Result<i64> {
        Ok(self.conn.query_row(
            "SELECT COUNT(*) FROM chunks c JOIN docs p ON p.id = c.doc_id
             WHERE p.deleted_at IS NULL",
            [],
            |r| r.get(0),
        )?)
    }

    /// Distinct indexed vocabulary, each word with its precomputed stem. The
    /// lexical ranker scans this with bounded Levenshtein for typo-tolerant
    /// word resolution (the vocabulary is small relative to postings - same
    /// idea as Meilisearch's word FST).
    ///
    /// Cached: the first call after a chunk write rebuilds it, every other
    /// call is a refcount bump. The old per-query `SELECT DISTINCT ... JOIN`
    /// walked all of `word_index` (millions of rows) on every search.
    ///
    /// It may contain words that only occur in soft-deleted docs. That is
    /// harmless: their postings and document frequency are always read with
    /// the live-doc filter, so such a word resolves to nothing.
    pub fn vocabulary(&self) -> Result<Arc<Vec<VocabWord>>> {
        if let Some(v) = self.vocab_cache.borrow().as_ref() {
            return Ok(v.clone());
        }
        // Loose index scan: jump from one distinct word to the next with an
        // index seek instead of visiting every posting.
        let mut stmt = self.conn.prepare(
            "WITH RECURSIVE w(word) AS (
                 SELECT MIN(word) FROM word_index
                 UNION ALL
                 SELECT (SELECT MIN(word) FROM word_index WHERE word > w.word)
                 FROM w WHERE w.word IS NOT NULL
             )
             SELECT word FROM w WHERE word IS NOT NULL",
        )?;
        let words = stmt
            .query_map([], |r| {
                let word: String = r.get(0)?;
                let stem = stem(&word);
                Ok(VocabWord { word, stem })
            })?
            .collect::<rusqlite::Result<Vec<VocabWord>>>()?;
        let words = Arc::new(words);
        *self.vocab_cache.borrow_mut() = Some(words.clone());
        Ok(words)
    }

    /// Drop the cached vocabulary; call after anything that writes chunks.
    pub(crate) fn invalidate_vocab(&self) {
        *self.vocab_cache.borrow_mut() = None;
    }

    /// Number of distinct live chunks containing an indexed word — the
    /// document frequency used by the lexical df-gate and IDF tier.
    pub fn word_chunk_count(&self, word: &str, excluded_dirs: &[&str]) -> Result<i64> {
        let exclude_sql = Self::exclude_clause(excluded_dirs);
        let sql = format!(
            "SELECT COUNT(DISTINCT w.chunk_id) FROM word_index w
             JOIN chunks c ON c.id = w.chunk_id
             JOIN docs p ON p.id = c.doc_id
             WHERE w.word = ?1 AND p.deleted_at IS NULL {exclude_sql}"
        );
        let mut stmt = self.conn.prepare_cached(&sql)?;
        Ok(stmt.query_row([word], |r| r.get(0))?)
    }

    /// SQL fragment excluding hard-excluded source dirs. `excluded_dirs` is a
    /// compile-time constant list (config::SourceTiers), never user input, so
    /// inlining it into SQL is safe.
    fn exclude_clause(excluded_dirs: &[&str]) -> String {
        if excluded_dirs.is_empty() {
            String::new()
        } else {
            let quoted: Vec<String> = excluded_dirs
                .iter()
                .map(|d| format!("'{}'", d.replace('\'', "''")))
                .collect();
            format!("AND p.source_dir NOT IN ({})", quoted.join(","))
        }
    }

    /// All postings for an exact indexed word, restricted to live docs and
    /// excluding hard-excluded source dirs (filtered at the SQL level).
    pub fn postings(&self, word: &str, excluded_dirs: &[&str]) -> Result<Vec<Posting>> {
        let exclude_sql = Self::exclude_clause(excluded_dirs);
        let sql = format!(
            "SELECT w.chunk_id, w.attr, w.pos FROM word_index w
             JOIN chunks c ON c.id = w.chunk_id
             JOIN docs p ON p.id = c.doc_id
             WHERE w.word = ?1 AND p.deleted_at IS NULL {exclude_sql}"
        );
        let mut stmt = self.conn.prepare_cached(&sql)?;
        let rows = stmt
            .query_map([word], |r| {
                Ok(Posting {
                    chunk_id: r.get(0)?,
                    attr: r.get(1)?,
                    pos: r.get(2)?,
                })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }
}
