import re

# Read the file
with open(r'C:\Users\imu\Desktop\ImuTechz\IMUCLI~1\imurun\frontend\design-lab\mockup.js', 'r') as f:
    content = f.read()

# Add Models tab between Server settings and Resource manager
old_subnav = '''          <button class="settings-tab" type="button" data-settings-section="server">Server settings</button>',
      '          <button class="settings-tab" type="button" data-settings-section="resources">Resource manager</button>','''

new_subnav = '''          <button class="settings-tab" type="button" data-settings-section="server">Server settings</button>',
      '          <button class="settings-tab" type="button" data-settings-section="models">Models</button>',
      '          <button class="settings-tab" type="button" data-settings-section="resources">Resource manager</button>','''

if old_subnav in content:
    content = content.replace(old_subnav, new_subnav)
    print("Added Models tab to subnav")
else:
    print("Could not find subnav pattern")
    # Try alternative
    alt_old = "'          <button class=\"settings-tab\" type=\"button\" data-settings-section=\"server\">Server settings</button>',\n      '          <button class=\"settings-tab\" type=\"button\" data-settings-section=\"resources\">Resource manager</button>',"
    alt_new = "'          <button class=\"settings-tab\" type=\"button\" data-settings-section=\"server\">Server settings</button>',\n      '          <button class=\"settings-tab\" type=\"button\" data-settings-section=\"models\">Models</button>',\n      '          <button class=\"settings-tab\" type=\"button\" data-settings-section=\"resources\">Resource manager</button>',"
    if alt_old in content:
        content = content.replace(alt_old, alt_new)
        print("Added with alternative pattern")
    else:
        print("Could not find alternative pattern either")

# Write back
with open(r'C:\Users\imu\Desktop\ImuTechz\IMUCLI~1\imurun\frontend\design-lab\mockup.js', 'w') as f:
    f.write(content)