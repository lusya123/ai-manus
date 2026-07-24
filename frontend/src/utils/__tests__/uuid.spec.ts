import { describe, expect, it, vi } from 'vitest'
import { createUuid } from '../uuid'

describe('createUuid', () => {
  it('uses crypto.randomUUID when the browser exposes it', () => {
    const expected = '123e4567-e89b-42d3-a456-426614174000'
    const randomUUID = vi.fn(() => expected)
    const cryptoProvider = {
      randomUUID,
      getRandomValues: vi.fn(),
    } as unknown as Crypto

    expect(createUuid(cryptoProvider)).toBe(expected)
    expect(randomUUID).toHaveBeenCalledOnce()
    expect(cryptoProvider.getRandomValues).not.toHaveBeenCalled()
  })

  it('falls back to getRandomValues on a non-secure HTTP origin', () => {
    const cryptoProvider = {
      getRandomValues: vi.fn((bytes: Uint8Array) => bytes.fill(0)),
    } as unknown as Crypto

    expect(createUuid(cryptoProvider)).toBe(
      '00000000-0000-4000-8000-000000000000',
    )
    expect(cryptoProvider.getRandomValues).toHaveBeenCalledOnce()
  })

  it('falls back when randomUUID is exposed but rejects the HTTP origin', () => {
    const cryptoProvider = {
      randomUUID: vi.fn(() => {
        throw new DOMException('Not allowed', 'SecurityError')
      }),
      getRandomValues: vi.fn((bytes: Uint8Array) => bytes.fill(0xff)),
    } as unknown as Crypto

    expect(createUuid(cryptoProvider)).toBe(
      'ffffffff-ffff-4fff-bfff-ffffffffffff',
    )
    expect(cryptoProvider.getRandomValues).toHaveBeenCalledOnce()
  })

  it('fails explicitly when no secure random source exists', () => {
    expect(() => createUuid(null)).toThrow(
      'Secure UUID generation is unavailable in this browser',
    )
  })
})
