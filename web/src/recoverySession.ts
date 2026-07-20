export const HOTEL_RECOVERY_SESSION_KEY = "backchannel.hotelRecoveryId";

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export interface HotelRecoveryHint {
  hadHint: boolean;
  recoveryId: string | null;
}

function browserSessionStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

export function isRecoveryUuid(value: string): boolean {
  return UUID_PATTERN.test(value);
}

export function clearHotelRecoveryHint(
  storage: Storage | null = browserSessionStorage(),
): void {
  if (storage === null) return;
  try {
    storage.removeItem(HOTEL_RECOVERY_SESSION_KEY);
  } catch {
    // Storage can be denied or unavailable without changing the recovery boundary.
  }
}

export function readHotelRecoveryHint(
  storage: Storage | null = browserSessionStorage(),
): HotelRecoveryHint {
  if (storage === null) return { hadHint: false, recoveryId: null };
  let value: string | null;
  try {
    value = storage.getItem(HOTEL_RECOVERY_SESSION_KEY);
  } catch {
    return { hadHint: false, recoveryId: null };
  }
  if (value === null) return { hadHint: false, recoveryId: null };
  if (!isRecoveryUuid(value)) {
    clearHotelRecoveryHint(storage);
    return { hadHint: true, recoveryId: null };
  }
  return { hadHint: true, recoveryId: value.toLowerCase() };
}

export function writeHotelRecoveryHint(
  recoveryId: string,
  storage: Storage | null = browserSessionStorage(),
): boolean {
  if (!isRecoveryUuid(recoveryId)) {
    clearHotelRecoveryHint(storage);
    return false;
  }
  if (storage === null) return false;
  try {
    storage.setItem(HOTEL_RECOVERY_SESSION_KEY, recoveryId.toLowerCase());
    return true;
  } catch {
    return false;
  }
}
