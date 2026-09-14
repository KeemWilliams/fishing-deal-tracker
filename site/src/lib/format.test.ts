import { describe, expect, it } from 'vitest';
import { displayProductName } from './format';

describe('displayProductName', () => {
  it('returns the real product name unchanged when it is not a placeholder', () => {
    expect(displayProductName('Triumph Spinning Rod', 'triumph-spinning-rod')).toBe('Triumph Spinning Rod');
  });

  it('titleizes the slug when the name is exactly "Default Title"', () => {
    expect(displayProductName('Default Title', 'vendetta-ice-spinning-rod')).toBe('Vendetta Ice Spinning Rod');
  });

  it('titleizes the slug when the name is empty', () => {
    expect(displayProductName('', 'power-pro-braided-line')).toBe('Power Pro Braided Line');
  });

  it('strips a leading "Default Title" prefix concatenated onto the real name', () => {
    // Real-world case (2026-09-13 Neon incident): some Shopify listings
    // produce a product name like "Default Title Vendetta Ice Spinning
    // Rod" rather than the name being exactly the placeholder.
    expect(displayProductName('Default Title Vendetta Ice Spinning Rod', 'default-title-vendetta-ice-spinning-rod')).toBe(
      'Vendetta Ice Spinning Rod'
    );
  });

  it('falls back to a titleized slug with the default-title- prefix stripped, when the name is only the placeholder', () => {
    expect(displayProductName('Default Title', 'default-title-some-reel')).toBe('Some Reel');
  });

  it('is case-insensitive when matching the placeholder', () => {
    expect(displayProductName('default title', 'ugly-stik-gx2')).toBe('Ugly Stik Gx2');
  });
});
