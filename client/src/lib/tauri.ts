import { invoke } from '@tauri-apps/api/core';

// Types mirror Rust structs from src-tauri/src/*.
export type PermissionState = 'granted' | 'denied' | 'not_determined' | 'unavailable';

export type PreFlightReport = {
	mic_permission: PermissionState;
	mic_device_present: boolean;
	mic_signal: boolean | null;
	system_device_present: boolean;
	error: string | null;
};

export type SpoolSession = {
	id: string;
	title: string;
	started_at: string;
	duration_sec: number;
	sample_rate: number;
	channels: number;
	mic_active: boolean;
	system_active: boolean;
	uploaded_offset: number;
	finalized: boolean;
};

// Thin command bindings; each adds error normalization on top of invoke.
export const commands = {
	preFlight(probe: boolean): Promise<PreFlightReport> {
		return invoke('cmd_pre_flight', { probe });
	},
	startRecording(title: string | null): Promise<string> {
		return invoke('cmd_start_recording', { title });
	},
	stopRecording(serverUrl: string | null, serverToken: string | null): Promise<SpoolSession> {
		return invoke('cmd_stop_recording', { serverUrl, serverToken });
	},
	uploadNow(baseUrl: string, token: string, sessionId: string): Promise<void> {
		return invoke('cmd_upload_now', { baseUrl, token, sessionId });
	},
	pump(): Promise<number> {
		return invoke('cmd_pump');
	}
};
