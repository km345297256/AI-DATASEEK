#!/usr/bin/env node
/** Generate approved adapter maps for the independently built host, backend and UI. */
import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const root = new URL('../', import.meta.url);
const sourcePath = 'contracts/visualization-adapters.json';
const source = JSON.parse(await readFile(new URL(sourcePath, root), 'utf8'));
const operationNames = ['bytes', 'page', 'preview', 'prepare', 'job'];
const inputModes = ['whole', 'page', 'prefix', 'window'];
const viewKinds = ['image', 'map', 'series', 'table', 'text', 'structure', 'document', 'tree', 'media', 'graph'];
const exact = (value, keys) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join(',') === [...keys].sort().join(',');
if (!exact(source, ['contract_version', 'adapters']) || source.contract_version !== 2
    || !source.adapters || !Object.keys(source.adapters).length) throw new Error('Invalid visualization adapter source');
for (const [adapter, spec] of Object.entries(source.adapters)) {
  if (!/^[a-z][a-z0-9-]*$/.test(adapter) || adapter.startsWith('v2-')
      || !exact(spec, ['readers', 'view_kind', 'capabilities'])
      || !Array.isArray(spec.readers) || !spec.readers.length
      || spec.readers.some(reader => typeof reader !== 'string' || !/^[a-z][a-z0-9-]*$/.test(reader))
      || new Set(spec.readers).size !== spec.readers.length || !viewKinds.includes(spec.view_kind)
      || !exact(spec.capabilities, ['operations', 'input_mode', 'shared'])
      || !Array.isArray(spec.capabilities.operations) || !spec.capabilities.operations.length
      || spec.capabilities.operations.some(operation => !operationNames.includes(operation))
      || new Set(spec.capabilities.operations).size !== spec.capabilities.operations.length
      || !inputModes.includes(spec.capabilities.input_mode) || typeof spec.capabilities.shared !== 'boolean') {
    throw new Error(`Invalid visualization adapter specification: ${adapter}`);
  }
}
const readers = [...new Set(Object.values(source.adapters).flatMap(spec => spec.readers))].sort();
const tsLiteral = values => values.map(value => JSON.stringify(value)).join(' | ');
const pythonLiteral = values => `Literal[${values.map(value => JSON.stringify(value)).join(', ')}]`;
const notice = `Generated from ${sourcePath}; do not edit. Run node scripts/sync-visualization-contract.mjs.`;
const typescript = `// ${notice}\nexport const VISUALIZATION_CONTRACT_VERSION = 2 as const;\nexport const VISUALIZATION_ADAPTERS = ${JSON.stringify(source.adapters, null, 2)} as const;\nexport type VisualizationAdapter = keyof typeof VISUALIZATION_ADAPTERS;\nexport type VisualizationReader = ${tsLiteral(readers)};\nexport type VisualizationOperation = ${tsLiteral(operationNames)};\nexport type VisualizationInputMode = ${tsLiteral(inputModes)};\nexport type VisualizationKind = ${tsLiteral(viewKinds)};\n`;
const python = `# ${notice}\nfrom typing import Literal\nimport json\n\nVISUALIZATION_CONTRACT_VERSION = 2\nVisualizationAdapter = ${pythonLiteral(Object.keys(source.adapters))}\nVisualizationReader = ${pythonLiteral(readers)}\nVisualizationOperation = ${pythonLiteral(operationNames)}\nVisualizationInputMode = ${pythonLiteral(inputModes)}\nVisualizationKind = ${pythonLiteral(viewKinds)}\nADAPTER_CONTRACTS = json.loads(r'''${JSON.stringify(source.adapters, null, 2)}''')\n`;
const generated = new Map([
  ['plugin-host/src/visualization-adapters.generated.ts', typescript],
  ['frontend/src/visualizations/adapters.generated.ts', typescript],
  ['backend/app/domain/models/visualization_adapters_generated.py', python],
]);
const args = process.argv.slice(2);
if (args.some(arg => arg !== '--check')) throw new Error('Usage: node scripts/sync-visualization-contract.mjs [--check]');
const mismatches = [];
for (const [path, content] of generated) {
  if (args.includes('--check')) {
    const current = await readFile(new URL(path, root), 'utf8').catch(() => null);
    if (current !== content) mismatches.push(path);
  } else await writeFile(new URL(path, root), content);
}
if (mismatches.length) {
  throw new Error(`Visualization contract generated files differ from ${sourcePath}: ${mismatches.join(', ')}. Run ${fileURLToPath(import.meta.url)}.`);
}
process.stdout.write(`Visualization adapter contract ${args.includes('--check') ? 'verified' : 'generated'} (${Object.keys(source.adapters).length} adapters).\n`);
