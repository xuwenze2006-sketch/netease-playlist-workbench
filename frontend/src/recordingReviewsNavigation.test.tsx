import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App from './App';

describe('recording reviews navigation', () => {
  it('opens the saved review catalog only when visited, using local reads', async () => {
    document.head.innerHTML = '<meta name="organizer-session" content="navigation-test" />';
    const fetcher = vi.fn().mockImplementation(async (url: string) => ({ ok: true, status: 200,
      json: async () => url === '/api/recording-reviews'
        ? { status: 'empty', bundles: [], limit: 100, truncated: false }
        : { connection: { installed: true, configured: true, authorized: null }, data: { source: 'empty', playlists: [] }, job: null } }));
    vi.stubGlobal('fetch', fetcher); render(<App />);
    await waitFor(() => expect(fetcher).toHaveBeenCalled());
    expect(fetcher.mock.calls.every(([url]) => url === '/api/state')).toBe(true);
    fireEvent.click(within(screen.getByRole('navigation', { name: '主导航' })).getByRole('link', { name: '录音核验' }));
    await waitFor(() => expect(window.location.hash).toBe('#recordings'));
    await waitFor(() => expect(fetcher.mock.calls.some(([url]) => url === '/api/recording-reviews')).toBe(true));
    expect(fetcher.mock.calls.every(([, config]) => config.method === 'GET')).toBe(true);
  });
});
