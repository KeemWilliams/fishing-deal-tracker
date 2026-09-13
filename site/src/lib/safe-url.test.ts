import { describe, expect, it } from 'vitest';
import { safeRetailerUrl } from './safe-url';

describe('safeRetailerUrl', () => {
  it('allows a valid https URL on the retailer allowlist', () => {
    expect(safeRetailerUrl('https://www.tacklewarehouse.com/sample/rod', 'tackle_warehouse')).toBe(
      'https://www.tacklewarehouse.com/sample/rod'
    );
  });

  it('allows the bare (non-www) allowlisted host', () => {
    expect(safeRetailerUrl('https://academy.com/sample/reel', 'academy')).toBe('https://academy.com/sample/reel');
  });

  it('rejects a javascript: URL', () => {
    expect(safeRetailerUrl('javascript:alert(1)', 'academy')).toBeNull();
  });

  it('rejects a data: URL', () => {
    expect(safeRetailerUrl('data:text/html,<script>alert(1)</script>', 'academy')).toBeNull();
  });

  it('rejects a protocol-relative //evil.com URL', () => {
    expect(safeRetailerUrl('//evil.com/phish', 'academy')).toBeNull();
  });

  it('rejects a plain http:// URL', () => {
    expect(safeRetailerUrl('http://www.academy.com/sample/reel', 'academy')).toBeNull();
  });

  it('rejects an https host not on that retailer\'s allowlist', () => {
    expect(safeRetailerUrl('https://evil.com/sample/reel', 'academy')).toBeNull();
  });

  it('rejects a host that belongs to a different retailer than the one claimed', () => {
    // Feed says "academy" but the URL points at tacklewarehouse.com -- must
    // not validate just because tacklewarehouse.com is on *some* allowlist.
    expect(safeRetailerUrl('https://www.tacklewarehouse.com/sample/reel', 'academy')).toBeNull();
  });

  it('rejects an unknown retailer slug entirely', () => {
    expect(safeRetailerUrl('https://www.academy.com/sample/reel', 'not_a_real_retailer')).toBeNull();
  });

  it('rejects an unparsable URL string', () => {
    expect(safeRetailerUrl('not a url', 'academy')).toBeNull();
  });

  it('accepts every configured retailer host', () => {
    const cases: Array<[string, string]> = [
      ['tackle_warehouse', 'https://www.tacklewarehouse.com/x'],
      ['academy', 'https://www.academy.com/x'],
      ['jandh', 'https://www.jandh.com/x'],
      ['fishusa', 'https://www.fishusa.com/x'],
      ['tackledirect', 'https://www.tackledirect.com/x'],
      ['alltackle', 'https://alltackle.com/x'],
    ];
    for (const [slug, url] of cases) {
      expect(safeRetailerUrl(url, slug)).toBe(url);
    }
  });
});
