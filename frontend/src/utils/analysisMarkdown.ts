import { Marked } from 'marked';

/** Single tildes express variable relationships/ranges in analysis text.
 * Keep explicit double-tilde deletion markup, without rewriting source text,
 * links, or code, or changing the global Markdown parser.
 */
export function createAnalysisMarkdown(): Marked {
  return new Marked({
    tokenizer: {
      del(source: string) {
        return source.startsWith('~~') ? false : undefined;
      },
    },
  });
}
