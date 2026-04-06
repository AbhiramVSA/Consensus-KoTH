// MongoDB seed script - insert users with crackable hashes
db = db.getSiblingDB('kothdb');

db.users.insertMany([
    {
        _id: 1,
        username: "admin",
        email: "admin@koth.local",
        // MD5 of "password123" - easily crackable
        password: "482c811da5d5b4bc6d497ffa98491e38",
        role: "administrator",
        created: new Date()
    },
    {
        _id: 2,
        username: "jsmith",
        email: "jsmith@koth.local",
        // MD5 of "letmein"
        password: "0d107d09f5bbe40cade3de5c71e9e9b7",
        role: "user",
        created: new Date()
    },
    {
        _id: 3,
        username: "sysadmin",
        email: "sysadmin@koth.local",
        // MD5 of "root" - the plaintext root password
        password: "63a9f0ea7bb98050796b649e85481845",
        note: "System admin - has root SSH access",
        ssh_password: "root",
        role: "sysadmin",
        created: new Date()
    }
]);

db.flags.insertOne({
    note: "Check /root/king.txt after getting root",
    hint: "mongouser has access to docker.sock - try: docker run -v /:/mnt alpine chroot /mnt sh"
});

print("MongoDB seeded successfully");
