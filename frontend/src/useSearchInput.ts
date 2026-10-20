import { useRef, useState } from 'react';
import type { ChangeEvent, CompositionEvent } from 'react';

export function useSearchInput() {
  const [value, setValue] = useState('');
  const [query, setQuery] = useState('');
  const composing = useRef(false);

  function confirm(next: string) {
    setValue(next);
    setQuery(next);
  }

  return {
    value,
    query,
    setValue(next: string) {
      composing.current = false;
      confirm(next);
    },
    onChange(event: ChangeEvent<HTMLInputElement>) {
      const next = event.currentTarget.value;
      setValue(next);
      if (!composing.current) setQuery(next);
    },
    onCompositionStart() { composing.current = true; },
    onCompositionEnd(event: CompositionEvent<HTMLInputElement>) {
      composing.current = false;
      confirm(event.currentTarget.value);
    },
  };
}
