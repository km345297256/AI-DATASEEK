export class CatalogValidationError extends Error {
  readonly code = -32010

  constructor(message: string) {
    super(message)
    this.name = 'CatalogValidationError'
  }
}

export class ProtocolError extends Error {
  constructor(
    readonly code: number,
    message: string,
  ) {
    super(message)
    this.name = 'ProtocolError'
  }
}
