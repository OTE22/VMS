# Camera credential autofill correction

New camera fields render blank. Application code only populates stored camera credentials in edit mode; passwords are represented by the redaction sentinel `***`. Generic username/password fields previously lacked autocomplete controls, making browser/password-manager login autofill a likely source of unexpected values. This diagnosis is based on source inspection, not observation of the user's browser.

The Builder form now disables autocomplete. Camera username/password inputs have camera-specific names, password `autocomplete="new-password"`, and ignore hints for common password managers. Existing IDs and the saved-camera edit behavior remain compatible. Password managers can override website hints; no claim is made that all extensions will obey them.

Validation: 59 frontend checks passed, including actual rendering of blank credential fields and saved-camera edit hydration. Following deployment, all 19 authenticated production page/API checks returned HTTP 200.

Release: `armyeye-vms:camera-autofill-20260922t043035z`.

Only the Builder template was deployed. Pending pipeline credential encryption remains undeployed.
