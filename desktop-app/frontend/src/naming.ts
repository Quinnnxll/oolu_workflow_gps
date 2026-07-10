// Concise, keyword-oriented names for nodes and task threads.
//
// A node's name is a label, not a transcript: "Convert the quarterly
// report to PDF and email it to accounting please" is a fine REQUEST and
// a terrible NAME. This module distills a task sentence into its
// load-bearing keywords — verbs and artifacts — so lists stay scannable
// while the full sentence remains available as the body/tooltip.

// Words that carry no identity: articles, pronouns, politeness, glue.
const STOPWORDS = new Set([
  "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "could",
  "do", "does", "for", "from", "get", "go", "has", "have", "hi", "how",
  "i", "in", "into", "is", "it", "its", "just", "let", "like", "make",
  "me", "my", "need", "now", "of", "on", "onto", "or", "our", "out",
  "please", "so", "some", "than", "that", "the", "their", "them", "then",
  "there", "they", "this", "to", "up", "us", "want", "was", "we", "what",
  "when", "which", "will", "with", "would", "you", "your",
]);

const MAX_KEYWORDS = 4;

export function keywords(text: string, limit = MAX_KEYWORDS): string[] {
  const seen = new Set<string>();
  const picked: string[] = [];
  for (const raw of (text ?? "").toLowerCase().split(/[^\p{L}\p{N}]+/u)) {
    const word = raw.trim();
    if (!word || STOPWORDS.has(word) || seen.has(word)) continue;
    seen.add(word);
    picked.push(word);
    if (picked.length >= limit) break;
  }
  return picked;
}

// "convert the quarterly report to pdf and email it" -> "Convert Quarterly
// Report Pdf". Falls back to a trimmed slice of the original text when
// nothing survives the stopword filter (e.g. "do it for me").
export function conciseName(text: string, limit = MAX_KEYWORDS): string {
  const words = keywords(text, limit);
  if (words.length === 0) {
    const fallback = (text ?? "").trim();
    return fallback.length > 32 ? `${fallback.slice(0, 32)}…` : fallback;
  }
  return words
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}
