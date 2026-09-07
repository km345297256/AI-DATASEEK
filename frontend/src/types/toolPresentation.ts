export type ToolPresentationKind =
  | 'auto'
  | 'generic'
  | 'table'
  | 'chart'
  | 'map'
  | 'image'
  | 'artifact'
  | 'log';

export type ToolChartType = 'line' | 'bar' | 'area' | 'scatter';
export type ToolLogLevel = 'debug' | 'info' | 'warning' | 'error';

export interface ToolPresentationColumn {
  key: string;
  label?: string | null;
  align?: 'left' | 'center' | 'right' | null;
}

export interface ToolPresentationSeries {
  key: string;
  label?: string | null;
  color?: string | null;
}

/**
 * A data-only display contract. It cannot select/import components or contain
 * executable markup. Unknown properties received from older/newer servers are
 * ignored by the normalizer before rendering.
 */
export interface ToolPresentation {
  kind: ToolPresentationKind;
  title?: string | null;
  description?: string | null;
  data?: unknown;
  columns?: ToolPresentationColumn[] | null;
  series?: ToolPresentationSeries[] | null;
  x_key?: string | null;
  chart_type?: ToolChartType | null;
  url?: string | null;
  mime_type?: string | null;
  filename?: string | null;
  level?: ToolLogLevel | null;
}
