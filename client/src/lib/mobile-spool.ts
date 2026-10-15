/** IndexedDB persistence for Android pending uploads (the mobile spool).
 *
 * Android takes live only in a module-level `$state` array — a crash or
 * app kill between "recording finished" and "upload finished" lost the
 * audio for good. Every outbox entry is written here before its upload
 * starts and removed once the server accepts it (or the user discards
 * it), so `initUploadTracking` can restore interrupted takes on the next
 * launch. Desktop does not use this store: its spool already survives
 * restarts on disk (src-tauri/src/spool.rs).
 *
 * Blobs are structured-cloneable, so the audio itself is persisted — no
 * separate file copy needed. Every helper is a safe no-op when
 * `indexedDB` is unavailable (plain-browser dev, private-mode WebView). */

const DB_NAME = 'transcripter-mobile-spool';
const STORE = 'outbox';

/** Persisted shape of one pending take. Mirrors `MobilePendingUpload`
 * in stores.svelte.ts (kept structural so the store type stays the
 * single source the Record/Library pages see). */
export type MobileOutboxItem = {
	id: string;
	blob: Blob;
	filename: string;
	title: string;
	tags: string[];
	recType: string | null;
	durationSec: number;
	createdAt: number;
	status: 'uploading' | 'failed';
	error: string;
};

function openOutboxDb(): Promise<IDBDatabase> {
	const { promise, resolve, reject } = Promise.withResolvers<IDBDatabase>();
	const request = indexedDB.open(DB_NAME, 1);
	request.onupgradeneeded = () => {
		if (!request.result.objectStoreNames.contains(STORE)) {
			request.result.createObjectStore(STORE, { keyPath: 'id' });
		}
	};
	request.onsuccess = () => resolve(request.result);
	request.onerror = () => reject(request.error ?? new Error('indexedDB open failed'));
	return promise;
}

function outboxAvailable(): boolean {
	return typeof indexedDB !== 'undefined';
}

/** Upsert one pending take (call before its upload starts and whenever
 * its status/error changes, so the persisted copy matches the UI). */
export async function saveMobileOutboxItem(item: MobileOutboxItem): Promise<void> {
	if (!outboxAvailable()) return;
	const db = await openOutboxDb();
	try {
		const { promise, resolve, reject } = Promise.withResolvers<void>();
		const tx = db.transaction(STORE, 'readwrite');
		tx.objectStore(STORE).put(item);
		tx.oncomplete = () => resolve();
		tx.onerror = () => reject(tx.error ?? new Error('indexedDB write failed'));
		tx.onabort = () => reject(tx.error ?? new Error('indexedDB write aborted'));
		await promise;
	} finally {
		db.close();
	}
}

/** Drop one entry: upload accepted by the server, or the user discarded
 * the take. */
export async function removeMobileOutboxItem(id: string): Promise<void> {
	if (!outboxAvailable()) return;
	const db = await openOutboxDb();
	try {
		const { promise, resolve, reject } = Promise.withResolvers<void>();
		const tx = db.transaction(STORE, 'readwrite');
		tx.objectStore(STORE).delete(id);
		tx.oncomplete = () => resolve();
		tx.onerror = () => reject(tx.error ?? new Error('indexedDB delete failed'));
		tx.onabort = () => reject(tx.error ?? new Error('indexedDB delete aborted'));
		await promise;
	} finally {
		db.close();
	}
}

/** All persisted takes, oldest first. Empty when the store is missing
 * (first launch) or indexedDB is unavailable. */
export async function loadMobileOutboxItems(): Promise<MobileOutboxItem[]> {
	if (!outboxAvailable()) return [];
	const db = await openOutboxDb();
	try {
		const { promise, resolve, reject } = Promise.withResolvers<MobileOutboxItem[]>();
		const tx = db.transaction(STORE, 'readonly');
		const request = tx.objectStore(STORE).getAll();
		request.onsuccess = () => {
			const items = (request.result ?? []) as MobileOutboxItem[];
			items.sort((a, b) => a.createdAt - b.createdAt);
			resolve(items);
		};
		request.onerror = () => reject(request.error ?? new Error('indexedDB read failed'));
		tx.onabort = () => reject(tx.error ?? new Error('indexedDB read aborted'));
		const items = await promise;
		return items;
	} finally {
		db.close();
	}
}
