use std::ffi::{c_void, CStr};
use std::ptr::NonNull;
use std::sync::atomic::{AtomicU32, Ordering};

use cpal::traits::{DeviceTrait, HostTrait};
use objc2::{rc::Retained, AnyThread};
use objc2_core_audio::{
    kAudioAggregateDeviceNameKey, kAudioAggregateDeviceTapAutoStartKey,
    kAudioAggregateDeviceTapListKey, kAudioAggregateDeviceUIDKey, kAudioEndPointDeviceIsPrivateKey,
    kAudioSubTapDriftCompensationKey, kAudioSubTapUIDKey, AudioHardwareCreateAggregateDevice,
    AudioHardwareCreateProcessTap, AudioHardwareDestroyAggregateDevice,
    AudioHardwareDestroyProcessTap, AudioObjectID, CATapDescription, CATapMuteBehavior,
};
use objc2_core_foundation::{
    kCFAllocatorDefault, kCFBooleanTrue, kCFTypeArrayCallBacks, kCFTypeDictionaryKeyCallBacks,
    kCFTypeDictionaryValueCallBacks, CFArray, CFBoolean, CFDictionary, CFMutableArray,
    CFMutableDictionary, CFRetained, CFString,
};
use objc2_foundation::{NSArray, NSNumber, NSString};


mod tcc {
    //! System Audio Recording permission (`kTCCServiceAudioCapture`) via the
    //! private TCC SPI.
    //!
    //! A Core Audio process tap is created successfully with or without this
    //! permission — but without it the HAL delivers all-zero buffers and no
    //! API call ever reports an error. The system prompt is normally triggered
    //! by starting IO on a tap-backed aggregate, but that requires a stable
    //! signing identity; our unsigned/ad-hoc builds never see it. The SPI
    //! used here (TCCAccessPreflight / TCCAccessRequest) is the same one
    //! AudioCap, screenpipe and cpal#1257 use to make this failure mode
    //! explicit.

    use std::ffi::c_void;
    use std::sync::mpsc;
    use std::sync::LazyLock;

    use block2::StackBlock;
    use objc2_core_foundation::{CFRetained, CFString};

    const SERVICE: &str = "kTCCServiceAudioCapture";

    type Preflight = unsafe extern "C" fn(*const c_void, *const c_void) -> i32;
    type Request = unsafe extern "C" fn(*const c_void, *const c_void, *const c_void);

    struct Tcc {
        preflight: Preflight,
        request: Request,
    }

    /// Loaded once; `None` when the private framework or symbols are absent
    /// (non-macOS-minus-SPI futures, hardened test hosts).
    static TCC: LazyLock<Option<Tcc>> = LazyLock::new(|| unsafe {
        let handle = libc::dlopen(
            c"/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC".as_ptr().cast(),
            libc::RTLD_LAZY | libc::RTLD_LOCAL,
        );
        if handle.is_null() {
            return None;
        }
        let preflight = libc::dlsym(handle, c"TCCAccessPreflight".as_ptr().cast());
        let request = libc::dlsym(handle, c"TCCAccessRequest".as_ptr().cast());
        if preflight.is_null() || request.is_null() {
            return None;
        }
        Some(Tcc {
            preflight: std::mem::transmute::<*mut c_void, Preflight>(preflight),
            request: std::mem::transmute::<*mut c_void, Request>(request),
        })
    });

    /// Non-blocking status check; never shows UI.
    /// `true` only when the permission is already granted.
    pub fn granted() -> bool {
        let Some(tcc) = TCC.as_ref() else {
            return false;
        };
        let service = service_string();
        unsafe { (tcc.preflight)(std::ptr::from_ref(&*service).cast(), std::ptr::null()) == 0 }
    }

    /// Shows the system prompt if undetermined; blocks until the user answers.
    /// `false` on denial, on an already-denied state, or if TCC is unavailable.
    pub fn request() -> bool {
        if granted() {
            return true;
        }
        let Some(tcc) = TCC.as_ref() else {
            return false;
        };
        let (tx, rx) = mpsc::sync_channel(1);
        let completion = StackBlock::new(move |granted: u8| {
            tx.send(granted != 0).ok();
        });
        let service = service_string();
        unsafe {
            (tcc.request)(
                std::ptr::from_ref(&*service).cast(),
                std::ptr::null(),
                std::ptr::from_ref(&completion).cast(),
            );
        }
        rx.recv().unwrap_or(false)
    }

    fn service_string() -> CFRetained<CFString> {
        CFString::from_str(SERVICE)
    }
}
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
    // The tap itself never reports a missing System Audio Recording
    // permission — the HAL would happily deliver all-zero buffers forever.
    // Make the permission explicit instead: prompt once, fail loudly on
    // denial so the recording degrades to mic-only with a real reason.
    if !tcc::request() {
        return Err(
            "system audio permission denied — allow it in System Settings \
             → Privacy & Security → System Audio Recording"
                .into(),
        );
    }
    let output_id = output.id().map_err(|e| e.to_string())?;
    let device_uid = NSString::from_str(output_id.id());
    // macOS 26 (Tahoe) rewrote Foundation in Swift: `+[NSArray new]`
    // dispatches into the Swift-backed class cluster and aborts a pure
    // Rust binary (_objc_fatal in lookUpImpOrForward). Build the empty
    // process list with the CoreFoundation C constructor instead — the
    // same toll-free-bridged object, no ObjC message dispatch.
    let processes = unsafe {
        CFArray::new(
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
        &*((&*processes as *const CFArray).cast::<NSArray<NSNumber>>())
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
            "Transcriptor Maximus system audio {pid}.{serial}"
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
    // Private aggregate devices appear in the host's device list
    // asynchronously; a cold HAL (first app launch) can take well over a
    // second. Poll briefly before declaring the aggregate undiscoverable.
    let mut device = None;
    for _ in 0..40 {
        if let Some(found) = host.device_by_id(&aggregate_device_id) {
            device = Some(found);
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
    let Some(device) = device else {
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
        let drift: &CFBoolean = kCFBooleanTrue.expect("kCFBooleanTrue is always available");
        CFMutableDictionary::set_value(
            Some(&dict),
            &*to_cfstring(kAudioSubTapDriftCompensationKey) as *const _ as *const c_void,
            drift as *const CFBoolean as *const c_void,
        );
        dict
    };

    // A 1-element immutable CFArray still routes through the Swift-backed
    // `__NSSingleObjectArrayI` class cluster on macOS 26 (Tahoe) and aborts
    // a pure-Rust binary; a mutable CFArray is a plain CoreFoundation class
    // with no ObjC dispatch on creation.
    let taps = unsafe {
        let arr = CFMutableArray::new(kCFAllocatorDefault, 1, &kCFTypeArrayCallBacks)
            .expect("Core Foundation array allocation failed");
        CFMutableArray::append_value(Some(&arr), &*tap_entry as *const _ as *const c_void);
        CFRetained::cast_unchecked::<CFArray>(arr)
    };

    unsafe {
        let dict = CFMutableDictionary::new(
            kCFAllocatorDefault,
            5,
            &kCFTypeDictionaryKeyCallBacks,
            &kCFTypeDictionaryValueCallBacks,
        )
        .expect("Core Foundation dictionary allocation failed");
        let name = CFString::from_str("Transcriptor Maximus loopback");
        let uid = CFString::from_str(aggregate_uid);
        // `kCFBooleanTrue` is toll-free bridged to NSNumber and avoids the
        // Swift-backed NSNumber class cluster on macOS 26 (Tahoe).
        let yes: &CFBoolean = kCFBooleanTrue.expect("kCFBooleanTrue is always available");
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
            yes as *const CFBoolean as *const c_void,
        );
        CFMutableDictionary::set_value(
            Some(&dict),
            &*to_cfstring(kAudioEndPointDeviceIsPrivateKey) as *const _ as *const c_void,
            yes as *const CFBoolean as *const c_void,
        );
        CFRetained::cast_unchecked::<CFDictionary>(dict)
    }
}
