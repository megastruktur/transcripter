// See https://svelte.dev/docs/kit/types#app.d.ts
declare global {
	/** Injected by vite `define` from package.json (see vite.config.ts). */
	const __APP_VERSION__: string;

	namespace App {
		// interface Error {}
		// interface Locals {}
		// interface PageData {}
		// interface PageState {}
		// interface Platform {}
	}
}

export {};
