$fn = 40;

module leg(){
difference(){

hull(){
    cylinder(d=16,h=32.75);
    translate([33,0,0])cylinder(d=8,h=32.75);
    translate([-8,0,0])cube([8,8,32.75]);
    translate([-8,6,0])cylinder(d=4,h=32.75);
    
}
cylinder(d=5.2,h=35);
}
}

cylinder(d=20,h=13.75);
