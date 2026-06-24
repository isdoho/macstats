// macstat-sensors — dumps Apple Silicon HID temperature sensors as JSON.
// Based on the IOHIDEventSystemClient pattern used by Stats.app.
//
// Build:
//   clang -fobjc-arc -framework Foundation -framework IOKit \
//         -o macstat-sensors macstat-sensors.m

#import <Foundation/Foundation.h>
#include <IOKit/hidsystem/IOHIDEventSystemClient.h>

typedef struct __IOHIDEvent *IOHIDEventRef;
typedef struct __IOHIDServiceClient *IOHIDServiceClientRef;

#define IOHIDEventFieldBase(type) (type << 16)
#define kIOHIDEventTypeTemperature 15

IOHIDEventSystemClientRef IOHIDEventSystemClientCreate(CFAllocatorRef allocator);
int IOHIDEventSystemClientSetMatching(IOHIDEventSystemClientRef client, CFDictionaryRef match);
CFArrayRef IOHIDEventSystemClientCopyServices(IOHIDEventSystemClientRef client);
IOHIDEventRef IOHIDServiceClientCopyEvent(IOHIDServiceClientRef svc, int64_t type, int32_t opts, int64_t ts);
CFTypeRef IOHIDServiceClientCopyProperty(IOHIDServiceClientRef svc, CFStringRef prop);
double IOHIDEventGetFloatValue(IOHIDEventRef event, int32_t field);

int main(void) {
    @autoreleasepool {
        NSDictionary *match = @{@"PrimaryUsagePage": @0xff00, @"PrimaryUsage": @0x0005};
        IOHIDEventSystemClientRef sys = IOHIDEventSystemClientCreate(kCFAllocatorDefault);
        if (!sys) { fputs("{}\n", stdout); return 0; }
        IOHIDEventSystemClientSetMatching(sys, (__bridge CFDictionaryRef)match);
        CFArrayRef services = IOHIDEventSystemClientCopyServices(sys);
        if (!services) { fputs("{}\n", stdout); CFRelease(sys); return 0; }

        NSMutableDictionary *out = [NSMutableDictionary dictionary];
        CFIndex count = CFArrayGetCount(services);
        for (CFIndex i = 0; i < count; i++) {
            IOHIDServiceClientRef svc = (IOHIDServiceClientRef)CFArrayGetValueAtIndex(services, i);
            NSString *name = CFBridgingRelease(IOHIDServiceClientCopyProperty(svc, CFSTR("Product")));
            IOHIDEventRef event = IOHIDServiceClientCopyEvent(svc, kIOHIDEventTypeTemperature, 0, 0);
            if (!name || !event) {
                if (event) CFRelease(event);
                continue;
            }
            double v = IOHIDEventGetFloatValue(event, IOHIDEventFieldBase(kIOHIDEventTypeTemperature));
            if (v > 0 && v < 110) out[name] = @(v);
            CFRelease(event);
        }
        CFRelease(services);
        CFRelease(sys);

        NSError *err = nil;
        NSData *json = [NSJSONSerialization dataWithJSONObject:out options:0 error:&err];
        if (!json) { fputs("{}\n", stdout); return 1; }
        fwrite(json.bytes, 1, json.length, stdout);
        fputc('\n', stdout);
    }
    return 0;
}
