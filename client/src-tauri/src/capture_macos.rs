use std::ffi::{c_void, CStr};
use std::ptr::NonNull;
use std::sync::atomic::{AtomicU32, Ordering};

use cpal::traits::{DeviceTrait, HostTrait};
use objc2::rc::Retained;
use objc2_core_audio::{
    kAudioAggregateDeviceNameKey, kAudioAggregateDeviceTapAutoStartKey,
    kAudioAggregateDeviceTapListKey, kAudioAggregateDeviceUIDKey, kAudioEndPointDeviceIsPrivateKey,
    kAudioSubTapDriftCompensationKey, kAudioSubTapUIDKey, AudioHardwareCreateAggregateDevice,
    AudioHardwareCreateProcessTap, AudioHardwareDestroyAggregateDevice,
    AudioHardwareDestroyProcessTap, AudioObjectID, CATapDescription, CATapMuteBehavior,
};
use objc2_core_foundation::{
    kCFAllocatorDefault, kCFBooleanTrue, kCFTypeArrayCallBacks, kCFTypeDictionaryKeyCallBacks,
    kCFTypeDictionaryValueCallBacks, CFArray, CFDictionary, CFMutableDictionary, CFRetained,
    CFString,
};
use objc2_foundation::{NSArray, NSNumber, NSString};

static INSTANCE: AtomicU32 = AtomicU32::new(0);

pub struct MacLoopbackDevice {
    tap_id: AudioObjectID,
    aggregate_id: AudioObjectID,
    device: cpal::Device,
}

impl MacLoopbackDevice {
    pub fn device(&self) -> &cpal::Device {
        &self.device
    }
}

impl Drop for MacLoopbackDevice {
    fn drop(&mut self) {
        unsafe {
            let _ = AudioHardwareDestroyAggregateDevice(self.aggregate_id);
            let _ = AudioHardwareDestroyProcessTap(self.tap_id);
        }
    }
}

pub fn create_loopback(output: &cpal::Device) -> Result<MacLoopbackDevice, String> {
    let output_id = output.id().map_err(|e| e.to_string())?;
    let device_uid = NSString::from_str(output_id.id());
    // macOS 26 (Tahoe) rewrote Foundation in Swift: `+[NSArray new]`
    // dispatches into the Swift-backed class cluster and aborts a pure
    // Rust binary (_objc_fatal in lookUpImpOrForward). Build the empty
    // process list with the CoreFoundation C constructor instead — the
    // same toll-free-bridged object, no ObjC message dispatch.
    let processes = unsafe {
        CFArray::<NSNumber>::new(
            kCFAllocatorDefault,
            std::ptr::null_mut(),
            0,
            &kCFTypeArrayCallBacks,
        )
        .expect("Core Foundation array allocation failed")
    };
    // SAFETY: CFArray and NSArray are toll-free bridged; mirrors the
    // AsRef conversion objc2-foundation generates for the same cast.
    let processes: &NSArray<NSNumber> = unsafe {
        &*((&*processes as *const CFArray<NSNumber>).cast::<NSArray<NSNumber>>())
    };
    let tap = unsafe {
        CATapDescription::initWithProcesses_andDeviceUID_withStream(
            CATapDescription::alloc(),
            processes,
            &device_uid,
            0,
        )
    };
    let serial = INSTANCE.fetch_add(1, Ordering::Relaxed);
    let pid = std::process::id();
    unsafe {
        tap.setMuteBehavior(CATapMuteBehavior::Unmuted);
        tap.setName(&NSString::from_str(&format!(
            "Transcripter system audio {pid}.{serial}"
        )));
        tap.setPrivate(true);
        tap.setExclusive(true);
    }

    let mut tap_id = 0;
    check_status(unsafe { AudioHardwareCreateProcessTap(Some(&tap), &mut tap_id) })?;

    let tap_uid = unsafe { tap.UUID().UUIDString() };
    let aggregate_uid = format!("com.megastruktur.transcripter.loopback.{pid}.{serial}");
    let properties = aggregate_properties(tap_uid, &aggregate_uid);
    let mut aggregate_id = 0;
    if let Err(error) = check_status(unsafe {
        AudioHardwareCreateAggregateDevice(&properties, NonNull::from(&mut aggregate_id))
    }) {
        unsafe {
            let _ = AudioHardwareDestroyProcessTap(tap_id);
        }
        return Err(error);
    }

    let host = cpal::default_host();
    let aggregate_device_id =
        cpal::DeviceId::new(cpal::platform::HostId::CoreAudio, &aggregate_uid);
    let Some(device) = host.device_by_id(&aggregate_device_id) else {
        unsafe {
            let _ = AudioHardwareDestroyAggregateDevice(aggregate_id);
            let _ = AudioHardwareDestroyProcessTap(tap_id);
        }
        return Err("Core Audio loopback aggregate was not discoverable".into());
    };

    Ok(MacLoopbackDevice {
        tap_id,
        aggregate_id,
        device,
    })
}

fn check_status(status: i32) -> Result<(), String> {
    if status == 0 {
        Ok(())
    } else {
        Err(format!("Core Audio error {status}"))
    }
}

fn to_cfstring(value: &'static CStr) -> CFRetained<CFString> {
    unsafe {
        CFString::with_c_string(kCFAllocatorDefault, value.as_ptr(), 0x0800_0100)
            .expect("static Core Audio key is valid UTF-8")
    }
}

fn aggregate_properties(
    tap_uid: Retained<NSString>,
    aggregate_uid: &str,
) -> CFRetained<CFDictionary> {
    let tap_entry = unsafe {
        let dict = CFMutableDictionary::new(
            kCFAllocatorDefault,
            2,
            &kCFTypeDictionaryKeyCallBacks,
            &kCFTypeDictionaryValueCallBacks,
        )
        .expect("Core Foundation dictionary allocation failed");
        CFMutableDictionary::set_value(
            Some(&dict),
            &*to_cfstring(kAudioSubTapUIDKey) as *const _ as *const c_void,
            &*tap_uid as *const _ as *const c_void,
        );
        // `kCFBooleanTrue` is toll-free bridged to NSNumber and avoids the
        // Swift-backed NSNumber class cluster on macOS 26 (Tahoe).
        let drift = kCFBooleanTrue.expect("kCFBooleanTrue is always available");
        CFMutableDictionary::set_value(
            Some(&dict),
            &*to_cfstring(kAudioSubTapDriftCompensationKey) as *const _ as *const c_void,
            &*drift as *const _ as *const c_void,
        );
        dict
    };
    let taps = unsafe {
        CFArray::new(
            kCFAllocatorDefault,
            [&tap_entry].as_ptr() as *mut *const c_void,
            1,
            &kCFTypeArrayCallBacks,
        )
        .expect("Core Foundation array allocation failed")
    };

    unsafe {
        let dict = CFMutableDictionary::new(
            kCFAllocatorDefault,
            5,
            &kCFTypeDictionaryKeyCallBacks,
            &kCFTypeDictionaryValueCallBacks,
        )
        .expect("Core Foundation dictionary allocation failed");
        let name = CFString::from_str("Transcripter loopback");
        let yes = kCFBooleanTrue.expect("kCFBooleanTrue is always available");
        CFMutableDictionary::set_value(
            Some(&dict),
            &*to_cfstring(kAudioAggregateDeviceNameKey) as *const _ as *const c_void,
            &*name as *const _ as *const c_void,
        );
        CFMutableDictionary::set_value(
            Some(&dict),
            &*to_cfstring(kAudioAggregateDeviceUIDKey) as *const _ as *const c_void,
            &*uid as *const _ as *const c_void,
        );
        CFMutableDictionary::set_value(
            Some(&dict),
            &*to_cfstring(kAudioAggregateDeviceTapListKey) as *const _ as *const c_void,
            &*taps as *const _ as *const c_void,
        );
        CFMutableDictionary::set_value(
            Some(&dict),
            &*to_cfstring(kAudioAggregateDeviceTapAutoStartKey) as *const _ as *const c_void,
            &*yes as *const _ as *const c_void,
        );
        CFMutableDictionary::set_value(
            Some(&dict),
            &*to_cfstring(kAudioEndPointDeviceIsPrivateKey) as *const _ as *const c_void,
            &*yes as *const _ as *const c_void,
        );
        CFRetained::cast_unchecked::<CFDictionary>(dict)
    }
}
