# Renderer Plugin Design

## Goal

Renderer plugins extend file previews without changing the conversation flow. When an agent produces a file, the frontend resolves a renderer by file extension and uses it to preview the file in the right-side file panel.

## Renderer Types

### Built-in renderer

Bundled Vue components registered in the frontend at build time.

Use this for trusted, common formats such as `png`, `jpeg`, `md`, `py`, and `csv`.

### API renderer

A backend or external service that receives a signed file URL or file bytes and returns a render result.

Recommended response shapes:

```json
{
  "kind": "html",
  "html": "<div>...</div>"
}
```

```json
{
  "kind": "image",
  "url": "https://..."
}
```

```json
{
  "kind": "json",
  "data": {}
}
```

API renderers should run server-side and return sanitized, display-only output.

### Component renderer

A packaged frontend component or script renderer. This must run in an isolated iframe sandbox, not in the main Vue app context.

Minimum sandbox requirements:

- No direct access to parent DOM.
- No access to auth tokens.
- Communication only through `postMessage`.
- Explicit allowlist for network access if needed.
- Renderer package must declare supported extensions and required permissions.

## Registry Shape

```ts
interface RendererDefinition {
  id: string;
  name: string;
  description: string;
  kind: 'builtin' | 'api' | 'component';
  extensions: string[];
  enabled: boolean;
}
```

## Resolution Order

1. Match enabled renderer by file extension.
2. Prefer user-configured renderer over built-in renderer.
3. Fall back to default hard-coded previews.
4. If no preview is available, show unknown-file preview with download action.

## PNG Example

The current implementation registers a built-in renderer:

```ts
{
  id: 'builtin-png-image',
  name: 'PNG Image Renderer',
  kind: 'builtin',
  extensions: ['png'],
  preview: ImageFilePreview,
  enabled: true
}
```

When a `.png` file is selected, the file panel resolves this renderer and renders the image using a signed download URL.

## Future Persistence

Renderer definitions should eventually be stored like MCP and Skills:

- Global renderers: available to all users.
- User renderers: private to one user.
- Disabled renderers: stored but not used.

Suggested endpoints:

- `GET /renderers`
- `POST /renderers`
- `PUT /renderers/{id}`
- `DELETE /renderers/{id}`

For component renderers, upload should accept a zip package with a manifest and static assets. The frontend should not execute arbitrary uploaded code directly in the main application bundle.
