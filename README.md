![logo](custom_components/tuya_local/brand/icon.svg) 

Please report any [issues](https://github.com/thomaskaergaard/tuya-local/issues) and feel free to raise [pull requests](https://github.com/thomaskaergaard/tuya-local/pulls).
[Many others](https://github.com/thomaskaergaard/tuya-local/blob/main/ACKNOWLEDGEMENTS.md) have contributed their help already.

[![BuyMeCoffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/jasonrumney)

This is a Home Assistant integration to support devices running Tuya
firmware without going via the Tuya cloud.  Devices are supported
over WiFi, limited support for devices connected via hubs is available.

Note that many Tuya devices seem to support only one local connection.
If you have connection issues when using this integration, ensure that
other integrations offering local Tuya connections are not configured
to use the same device, mobile applications on devices on the local
network are closed, and no other software is trying to connect locally
to your Tuya devices.

Using this integration does not stop your devices from sending status
to the Tuya cloud, so this should not be seen as a security measure,
rather it improves speed and reliability by using local connections,
and may unlock some features of your device, or even unlock whole
devices, that are not supported by the Tuya cloud API.

A similar but unrelated integration is
[rospogrigio/localtuya](https://github.com/rospogrigio/localtuya/), if
your device is not supported by this integration, you may find it
easier to set up using that, or another more recent fork, as an alternative.


---

## Installation

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://github.com/hacs/integration)

Installation is easiest via the [Home Assistant Community Store
(HACS)](https://hacs.xyz/), which is the best place to get third-party
integrations for Home Assistant. Once you have HACS set up, simply click the button below (requires My Homeassistant configured) or
follow the [instructions for adding a custom
repository](https://hacs.xyz/docs/faq/custom_repositories) and then
the integration will be available to install like any other.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=thomaskaergaard&repository=tuya-local&category=integration)

## Configuration

After installing, you can easily configure your devices using the Integrations configuration UI.  Go to Settings / Devices & Services and press the Add Integration button, or click the shortcut button below (requires My Homeassistant configured).

[![Add Integration to your Home Assistant
instance.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=tuya_local)

### Choose your configuration path

There are three options for configuring a device:
- Devices found automatically on your local network will appear on the Integrations page, ready to be added.
- You can login to Tuya cloud with the Tuya or SmartLife app and retrieve a list of devices and the necessary local connection data.
- You can provide all the necessary information manually [as per the instructions in DEVICES_DETAILS.md](DEVICE_DETAILS.md#finding-your-device-id-and-local-key).

The second choice essentially automates all the manual steps of the third and without needing to create a Tuya IOT developer account. This is especially important now that Tuya has started time limiting access to a key data access capability in the IOT developer portal to only a month with the ability to refresh the trial of that only every 6 months.

The cloud assisted choice will guide you through authenticating, choosing a device to add from the list of devices associated with your Tuya account, locate the device on your local subnet and then drop you into [Stage One](#stage-one) with fully populated data necessary to move forward to [Stage Two](#stage-two).

The Tuya authentication token is cached so that local keys can be refreshed without you needing to log in again. It expires after a small number of hours, after which you will be asked to authenticate again the next time it is needed.

### Automatic discovery

Tuya devices announce themselves on the local network, and the integration listens for those announcements. This means:

- Devices you have not added yet are offered on the Integrations page without you needing to search for them. If the local key is already known from a previous cloud login it is filled in for you, otherwise you will be asked for it.
- The same list is available on demand through the "Automatically discover devices on the local network" setup choice, which is useful if you dismissed the discovery notification or want to add several devices in a row.
- Devices that change IP address, for example after a DHCP lease expires, are updated automatically instead of becoming unavailable.

Discovery listens on UDP ports 6666, 6667 and 7000. If another application on the same host is already using those ports, discovery is skipped and everything else continues to work as before.

#### Quick add

When a discovered device is added, either from the Integrations page or through the automatic setup choice, "Add without further questions" is offered and is on by default. If the local key is already known and the device matches exactly one configuration perfectly, the device is added immediately under the name it has in the app, skipping the connection, configuration and naming steps.

Anything less certain still asks. If the key is unknown, or the connection fails, the usual connection form appears with what is known already filled in; if several configurations fit equally well, or the best one is only a partial match, you are asked to choose. Turn the option off to review every step, which is what you want when the device needs a name other than the one it has in the app.

Protocol 3.1 to 3.4 devices announce themselves unprompted, but 3.5 devices stay silent until they are asked, so a discovery request is also broadcast when Home Assistant starts, once a minute afterwards, and whenever the automatic setup choice is used.

#### Devices on another subnet or VLAN

Broadcasts are not forwarded between subnets, so devices on a separate VLAN are never heard. A device's answer to a discovery request does not help either, because it arrives as a new inbound connection which a firewall between the networks will normally drop.

Those networks are therefore searched by connecting to each address on the Tuya device port instead. That is ordinary outgoing traffic, so it is routed and allowed like any other connection Tuya Local makes to a device, and it works for every protocol version. List the networks in `configuration.yaml`:

```yaml
tuya_local:
  discovery_networks:
    - 192.168.3.0/24
    - 192.168.5.11
```

Each entry may be a single address or a CIDR range, and a range is limited to 1024 addresses. A whole `/24` takes about ten seconds to search. It is searched when the automatic setup choice is used, and every five minutes in the background so that devices which change address are repaired.

A device only identifies itself to someone who already holds its local key, so devices found this way are named once you have logged in to the cloud, and are otherwise offered as "Unknown device" with just their address. Choosing an unknown device asks which account it is registered in, and the keys from that account are then tried against the address, so the device id, local key and protocol version are filled in for you. Only if nothing in the account answers do you have to enter them by hand. The cloud assisted flow also searches these networks, so choosing a device from your Tuya account will find it on another VLAN without you needing to know its address.

Home Assistant only loads the integration once it has at least one device, so discovery begins after your first device has been added manually or through the cloud assisted flow.

### Devices sold under another brand

Some manufacturers ship their own app instead of SmartLife, so their devices never appear in a Tuya developer account and the SmartLife login cannot see them. Their apps talk to the same Tuya service, distinguished only by the brand's own credentials, so signing in with the brand account gives the local keys for those devices.

Choose "LEDVANCE or other brand cloud-assisted device setup", or pick the brand when a discovered device asks which account it is registered in, then enter the email address and password of the brand's app. The region has to be the one the account was created in.

The following brands are supported:

- LEDVANCE SMART+
- SYLVANIA Smart

The keys are cached, so devices from these accounts are named by discovery from then on. The password is not stored, which means these keys are not refreshed automatically the way SmartLife keys are; sign in again if a device is reset or re-paired.

If a device of this kind is only recognised as a generic configuration, or is not recognised at all, the brand account is also asked for its datapoint specification while the device is being added. That specification, the product id and the local datapoint values are all written to the Home Assistant log, and are what a configuration fitting the exact model can be written from, so include them when requesting support for a new device. The product id is recorded against the device as well, so it is reported in its diagnostics afterwards without having to search the log.

The description of this interface was published by [FlagX](https://github.com/FlagX/ha-ledvance-tuya-resync-localkey) under the MIT licence.

### Local key refresh

Tuya issues a new local key every time a device is reset or re-paired with the mobile app, which makes the stored key stop working. If a device fails to connect and a cached cloud login is available, the integration will fetch the current key from the cloud and repair the configuration entry by itself. Cloud lookups are rate limited, so a device that is merely switched off will not cause repeated cloud requests.

### Stage One

The first stage of configuration is to provide the information needed to connect to the device.

When using the cloud assisted config, the device id and local key will be pre-filled from the cloud, and the IP address will also be filled if local discovery is not blocked by other integrations or a complex network setup. Otherwise, see [DEVICE_DETAILS.md](DEVICE_DETAILS.md) for instructions on how to find the info.

#### host

&nbsp;&nbsp;&nbsp;&nbsp;_(string) (Required)_ IP or hostname of the device.

#### device_id

&nbsp;&nbsp;&nbsp;&nbsp;_(string) (Required)_ Device ID retrieved

#### local_key

&nbsp;&nbsp;&nbsp;&nbsp;_(string) (Required)_ Local key retrieved

Note that each time you pair the device, the local key changes, so if you obtained the local key using the instructions below, then re-paired with your manufacturer's app, then the key will have changed already.

#### protocol_version

&nbsp;&nbsp;&nbsp;&nbsp;_(string or float) (Required)_ Valid options are "auto", 3.1, 3.2, 3.3, 3.4, 3.5, 3.22.  If you aren't sure, choose "auto", but some 3.2, 3.22 and maybe 3.4 devices may be misdetected as 3.3 (or vice-versa), so if your device does not seem to respond to commands reliably, try selecting between those protocol versions. Protocol 3.22 is a special case, that enables tinytuya's "device22" detection with protocol 3.3. Previously we let tinytuya auto-detect this, but it was found to sometimes misdetect genuine 3.3 devices as device22 which stops them receiving updates, so an explicit version was added to enable the device22 detection.

At the end of this step, an attempt is made to connect to the device and see if
it returns any data. For tuya protocol version 3.1 devices, the local key is
only used for sending commands to the device, so if your local key is
incorrect the setup will appear to work, and you will not see any problems
until you try to control your device.  For more recent Tuya protocol versions,
the local key is used to decrypt received data as well, so an incorrect key
will be detected at this step and cause an immediate failure.


### Stage Two

The second stage of configuration is to select which device you are connecting.
The list of devices offered will be limited to devices which appear to be
at least a partial match to the data returned by the device.

#### type

&nbsp;&nbsp;&nbsp;&nbsp;_(string) (Optional)_ The type of Tuya device.
Select from the available options.

The list presented is filtered to exclude devices that definitely do not match among the 1000+ supported devices. If a device config you expected is not shown, you may have a different firmware version, so the best way to report this is as a new device.

If you pick the wrong type, you will need to delete the device and set
it up again. This is because different types of devices create different
entities, so changing the device type without deleting everything is
not advisable.

### Stage Three

The final stage is to choose a name for the device in Home Assistant.

If you have multiple devices of the same type, you may want to change
the name to make it easier to distinguish them.

#### name

&nbsp;&nbsp;&nbsp;&nbsp;_(string) (Required)_ Any unique name for the
device.  This will be used as the base for the entity names in Home
Assistant.

---

## Device support

A list of currently supported devices can be found in the [DEVICES.md](https://github.com/thomaskaergaard/tuya-local/blob/main/DEVICES.md) file.

Note that devices sometimes get firmware upgrades, or incompatible
versions are sold under the same model name, so it is possible that
the device will not work despite being listed.

Battery powered devices such as door and window sensors, smoke alarms
etc which do not use a hub are not possible to support locally, due
to the power management that they need to do to get acceptable battery
life. In some cases that may also apply when a device that can be
either battery or USB powered is plugged into USB. If you cannot gather
Warning level logs with dps listed when attempting to set it up, then it
will likely not work with this integration.

Hubs are currently supported, but with limitations.  Each connection
to a sub device uses a separate network connection, but like other
Tuya devices, hubs are usually limited in the number of connections
they can handle, with typical limits being 1 or 3, depending on the specific
Tuya module they are using.  This severely limits the number of sub devices
that can be connected through this integration.

Sub devices should be added using the `device_id`, `address` and `local_key`
of the hub they are attached to, and the `node_id` of the sub-device. If there
is no `node_id` listed, try using the `uuid` instead.

Tuya Zigbee devices are usually standard zigbee devices, so as an
alternative to this integration with a Tuya hub, you can use a
supported Zigbee USB stick or Wifi hub with
[ZHA](https://www.home-assistant.io/integrations/zha/#compatible-hardware)
or [Zigbee2MQTT](https://www.zigbee2mqtt.io/guide/adapters/).

Some Tuya Bluetooth devices can be supported directly by the
[tuya_ble](https://github.com/PlusPlus-ua/ha_tuya_ble/) integration.

Some Tuya hubs now support Matter over WiFi, and this can be used as an
alternative to this integration for connecting the hub and sub-devices
to Home Assistant. Other limitations will apply to this, so you might want
to try both, and only use this integration for devices that are not working
properly over Matter.

Tuya IR hubs that expose general IR remotes as sub devices usually
expose them as one way devices (send only) except in learning mode,
if they expose them at all locally. In general, Tuya IR hubs are only
useful for HA's built in IR support, not for any Tuya features such as their
predefined (cloud only) device database, or climate device simulation.

## Contributing

Documentation on building a device configuration file is in [/custom_components/tuya_local/devices/README.md](https://github.com/thomaskaergaard/tuya-local/blob/main/custom_components/tuya_local/devices/README.md)

If your device is not listed, you can find the information required to add a configuration for it in the following locations:

1. When attempting to add the device, if it is not supported, you will either get a message saying the device cannot be recognised at all, or you will be offered a list of devices that are partial matches. You can cancel the process at this point, and look in the Home Assistant log - there should be a message there containing the current data points (dps) returned by the device.
2. If you have signed up for [iot.tuya.com](https://iot.tuya.com/), you should have access to the API Explorer under "Cloud". Under "Device Control" there is a function called "Query Things Data Model", which returns the dp id in addition to range information that is needed for integer and enum data types.

If you file an issue to request support for a new device, please include the following information:

1. Logs from this integration showing the LOCAL DPS actually received from the device.
2. Identification of the device, such as model and brand name.
3. As much information on the datapoints you can gather using the above methods.
4. If manuals or webpages are available online, links to those help understand how to interpret the technical info above - even if they are not in English automatic translations can help, or information in them may help to identify identical devices sold under other brands in other countries that do have English or more detailed information available.

If you submit a pull request, please understand that the config file naming and details of the configuration may get modified before release - for example if your name was too generic, I may rename it to a more specific name, or conversely if the device appears to be generic and sold under many brands, I may change the brand specific name to something more general.  So it may be necessary to remove and re-add your device once it has been integrated into a release.

---

## Offline operation issues

Many Tuya devices will stop responding if unable to connect to the
Tuya servers for an extended period.  Reportedly, some devices act
better offline if DNS as well as TCP connections is blocked.

## General issues

Many Tuya devices do not handle multiple commands sent in quick
succession.  Some will reboot, possibly changing state in the process,
others will go offline for 30s to a few minutes if you overload them.
There is some rate limiting to try to avoid this, but it is not
sufficient for some devices, and may not work across entities where
you are sending commands to multiple entities on the same device.  The
rate limiting also combines commands, which not all devices can
handle. If you are sending commands from an automation, it is best to
add delays between commands - if your automation is for multiple
devices, it might be enough to send commands to other devices first
before coming back to send a second command to the first one, or you
may still need a delay after that.  The exact timing depends on the
device, so you may need to experiment to find the minimum delay that
gives reliable results.

Most devices can handle multiple commands in a single message, so for
entity platforms that support it (eg climate `set_temperature` can
include presets, lights pretty much everything is set through
`turn_on`) multiple settings are sent at once.  But some devices do
not like this and require all commands to set only a single dp at a
time, so you may need to experiment with your automations to see
whether a single command or multiple commands (with delays, see above)
work best with your devices.

When adding devices, some devices that are detected as protocol version
3.3 at first require version 3.2 to work correctly. Either they cannot be
detected, or work as read-only if the pprotocol is set to 3.3.

## Connecting to devices via hubs

If your device connects via a hub (eg. battery powered water timers) you have to provide the following info when adding a new device:

- Device id (uuid): this is the **hub's** device id
- IP address or hostname: the **hub's** IP address or hostname
- Local key: the **hub's** local key
- Sub device id: the **actual device you want to control's** `node_id`. Note this `node_id` differs from the device id, you can find it with tinytuya as described below.

## Secure locks

Many locks are designed with basic security controls to make remote unlocking
more difficult. This integration supports the standard BLE lock model from Tuya
which uses a pair of dps (60: `remote_no_pd_seykey`, 61: `remote_no_dp_key`)
to share a key between the app and the lock during the pairing phase.
If you have access to the Tuya developer portal, you can eavesdrop on the
second of these messages when the app is used to unlock the lock remotely.
If you capture the value sent by the app, then you can decode it using a base64
decoder such as https://base64decode.org.
The format has 4 bytes of binary data, followed by an 8 digit ASCII numeric
code, followed by 3 or 4 more bytes of binary data.

The 8 digit numeric code from the first app that was paired should work for
unlocking the lock.

Although this is documented in the BLE lock documentation from Tuya, Zigbee
and WiFi locks often use the same naming for datapoints, which may be
compatible with this scheme.

## IR/RF blasters

Tuya IR and RF blasters are exposed as remote entities and support learning and
sending commands via the standard Home Assistant remote services. IR blasters are
also exposed as general `infrared` emitters.

### Learning commands

Use the `remote.learn_command` service with:
- `command`: the name to store the command under (e.g. `power`)
- `device`: a name for the appliance being controlled (e.g. `TV`)
- `command_type`: set to `rf` for RF remotes, omit or leave blank for IR

The integration will put the blaster into learning mode and wait up to 30 seconds
for you to press a button on the original remote. The learned code is stored
persistently and survives restarts.

### Sending commands

Using the `infrared` platform, you can send known IR commands using
other HA integrations, including
[HAIR](https://github.com/DAB-LABS/HAIR), a custom integration for
learning remote commands via an ESPHome receiver and sending them to
any supported `infrared` emitter.

To send learned commands, you use the `remote.send_command` service
with the same `command` and `device` values used when learning. You
can also send known Tuya codes directly without learning first:

- **IR inline code**: prefix with `b64:` followed by the base64-encoded IR code
- **RF inline code**: prefix with `rf:` followed by the base64-encoded RF code

<!-- There is also a special `send_learned_ir_command` service for sending commands
learned by the `remote` entity to any `infrared` emitter (including non-Tuya ones). -->


### UI

If you would like to expose the learnt commands as buttons in the user interface
you might want to take a look at the [Remote buttons](https://github.com/kongo09/remote_buttons)
integration, which is compatible with Tuya Local.

## Pet feeders

Many pet feeders expose an encoded **Meal plan** setting via a text entity. By default this is disabled, but you can enable it under the Device settings in HA. When enabled many pet feeders share the same underlying format, which is supported by the [FrederikM97/mealplan-card](https://github.com/FredrikM97/mealplan-card) custom card.

## Contributing

Beyond contributing device configs, here are some areas that could benefit from more hands:

1. Unit tests. This integration is mostly unit-tested thanks to the upstream project, but there are a few more to complete. Focus on unit tests is on python code, the current coverage is summarised in reports on github, but to get full coverage details you can run the tests yourself.
2. Once unit tests are complete, the next task is to properly evaluate against the Home Assistant quality scale.
3. Discovery. Local discovery is currently limited to finding the IP address in the cloud assisted config. Performing discovery in background would allow notifications to be raised when new devices are noticed on the network, and would provide a productKey for the manual config method to use when matching device configs.

