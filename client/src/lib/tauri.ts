import { invoke } from '@tauri-apps/api/core';

// Types mirror Rust structs from src-tauri/src/*.
export type PermissionState = 'granted' | 'denied' | 'not_determined' | 'unavailable';

export type SourceState = 'disabled' | 'ready' | 'silent' | 'permission_denied' | 'unavailable' | 'failed';

export type PreFlightReport = {
	mic_permission: PermissionState;
	mic_state: SourceState;
	mic_signal: boolean | null;
	system_state: SourceState;
	system_signal: boolean | null;
	error: string | null;
};

export type AudioDeviceInfo = { id: string; label: string; is_default: boolean };

export type AudioDevices = {
	microphones: AudioDeviceInfo[];
	system_outputs: AudioDeviceInfo[];
	default_microphone: string | null;
	default_system_output: string | null;
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
	mic_dropped_frames: number;
	system_dropped_frames: number;
	mic_xruns: number;
	system_xruns: number;
	capture_error: string | null;
	uploaded_offset: number;
	server_rec_id?: string | null;
	finalized: boolean;
};

// Thin command bindings; each adds error normalization on top of invoke.
export const commands = {
	listAudioDevices(): Promise<AudioDevices> {
		return invoke('cmd_list_audio_devices');
	},
	preFlight(probe: boolean, microphone: string | null, systemOutput: string | null, checkSystem: boolean): Promise<PreFlightReport> {
		return invoke('cmd_pre_flight', { probe, microphone, systemOutput, checkSystem });
	},
	startRecording(title: string | null, microphone: string | null, systemOutput: string | null, captureSystem: boolean): Promise<string> {
		return invoke('cmd_start_recording', { title, microphone, systemOutput, captureSystem });
	},
	stopRecording(serverUrl: string | null, serverToken: string | null): Promise<SpoolSession> {
		return invoke('cmd_stop_recording', { serverUrl, serverToken });
	},
	uploadNow(baseUrl: string, token: string, sessionId: string): Promise<void> {
		return invoke('cmd_upload_now', { baseUrl, token, sessionId });
	},
	recordingFrames(): Promise<number> {
		return invoke('cmd_recording_frames');
	},
	retryPending(baseUrl: string, token: string): Promise<number> {
		return invoke('cmd_retry_pending', { baseUrl, token });
	}
};
