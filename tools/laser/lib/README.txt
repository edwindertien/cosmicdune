Put the Helios shared library in this folder (tools/laser/lib/):

  macOS : libHeliosLaserDAC.dylib  AND  libusb-1.0.0.dylib   (both, same folder)
          from helios_dac/sdk/cpp/shared_library/  and  helios_dac/sdk/cpp/libusb_bin/macOS/
  Linux / Raspberry Pi : libHeliosDacAPI.so, built from source (see SETUP.md step 5a;
          the .so in the SDK repo is x86-64 only)

Then:  python3 laser_test.py probe      (finds the DAC, emits no light)
