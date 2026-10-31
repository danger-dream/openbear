import flagFont from 'country-flag-emoji-polyfill/dist/TwemojiCountryFlags.woff2?url';
import { polyfillCountryFlagEmojis } from 'country-flag-emoji-polyfill';

export function loadFlagFont() { polyfillCountryFlagEmojis('Twemoji Country Flags', flagFont); }
