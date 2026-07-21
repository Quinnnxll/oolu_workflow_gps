// The reading order of a messenger, shared by the Friends and Noder
// groups: hidden threads leave the list (new words bring them back —
// the server clears the flag), pinned threads rise, and within each
// band the most recently spoken sits uppermost.

export interface ListedThread {
  pinned?: boolean;
  hidden?: boolean;
}

export function orderThreads<T extends ListedThread>(
  items: T[],
  moment: (item: T) => string,
): T[] {
  return items
    .filter((item) => !item.hidden)
    .sort((a, b) => {
      if (!!a.pinned !== !!b.pinned) return a.pinned ? -1 : 1;
      // ISO timestamps in one format compare lexicographically; an empty
      // moment (a silent fresh thread) sinks below anything spoken.
      return moment(b).localeCompare(moment(a));
    });
}
