import assert from 'node:assert/strict'
import test from 'node:test'
import { CatalogRuntime } from '../dist/catalog.js'

function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

test('concurrent shutdown callers join the same disposal and admission closes immediately', async () => {
  const runtime = new CatalogRuntime('/unused-synthetic-catalog')
  const entered = deferred()
  const release = deferred()
  const calls = []
  runtime.current = { context: { fiber: { dispose: async () => {
    calls.push('catalog')
    entered.resolve()
    await release.promise
  } } } }
  runtime.visualizations.shutdown = async () => { calls.push('visualizations') }
  const first = runtime.shutdown()
  const second = runtime.shutdown()
  assert.equal(first, second)
  await entered.promise
  await assert.rejects(runtime.reload(), /shutting down/)
  assert.deepEqual(calls, ['catalog'])
  release.resolve()
  await Promise.all([first, second])
  await runtime.shutdown()
  assert.deepEqual(calls, ['catalog', 'visualizations'])
})

test('catalog disposal failure does not skip visualization cleanup and remains observable', async () => {
  const runtime = new CatalogRuntime('/unused-synthetic-catalog')
  const failure = new Error('synthetic catalog failure')
  const calls = []
  runtime.current = { context: { fiber: { dispose: async () => {
    calls.push('catalog')
    throw failure
  } } } }
  runtime.visualizations.shutdown = async () => { calls.push('visualizations') }
  await assert.rejects(runtime.shutdown(), error => error === failure)
  await assert.rejects(runtime.shutdown(), error => error === failure)
  assert.deepEqual(calls, ['catalog', 'visualizations'])
})

test('all independent disposal failures are aggregated', async () => {
  const runtime = new CatalogRuntime('/unused-synthetic-catalog')
  const errors = [new Error('catalog'), new Error('visualizations')]
  runtime.current = { context: { fiber: { dispose: async () => { throw errors[0] } } } }
  runtime.visualizations.shutdown = async () => { throw errors[1] }
  await assert.rejects(runtime.shutdown(), error => {
    assert.ok(error instanceof AggregateError)
    assert.deepEqual(error.errors, errors)
    return true
  })
})

test('accepted catalog replacement finishes before its resulting context is disposed', async () => {
  const runtime = new CatalogRuntime('/unused-synthetic-catalog')
  const release = deferred()
  const calls = []
  runtime.operation = release.promise.then(() => {
    calls.push('replacement')
    runtime.current = { context: { fiber: { dispose: async () => { calls.push('catalog') } } } }
  })
  runtime.visualizations.shutdown = async () => { calls.push('visualizations') }
  const closing = runtime.shutdown()
  await Promise.resolve()
  assert.deepEqual(calls, [])
  release.resolve()
  await closing
  assert.deepEqual(calls, ['replacement', 'catalog', 'visualizations'])
})
